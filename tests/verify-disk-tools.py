#!/usr/bin/env python3
"""End-to-end check of the disk utility's helper against real block devices.

Run as root:    sudo ./tests/verify-disk-tools.py [--only SECTION ...]

No real drive is touched. Every device used is a loop device this script
creates itself from a sparse file in a temporary directory: to the kernel and
to every tool that is a disk like any other, with a real partition table and
real partitions, so what is exercised is the real helper running the real
mkfs, sfdisk and wipefs - not a stand-in. Each result is then checked
independently of the helper, with blkid, sfdisk, fsck and a mount.

The helper is driven through the same client the window uses, over a pipe,
and it is the copy in this source tree, not an installed one.

Every loop device and file is removed on the way out, whatever happens.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from ksyssupervisor import diskctl, diskfs, diskrecover, disks  # noqa: E402

HELPER = os.path.join(REPO, "helpers", "ksyssupervisor-diskhelper")
MIB = 1024 * 1024
GIB = 1024 * MIB

GREEN, RED, YELLOW, BOLD, NC = ("\033[0;32m", "\033[0;31m", "\033[1;33m",
                                "\033[1m", "\033[0m")
PASSED, FAILED, SKIPPED = [], [], []


def passed(name):
    print("  %sPASS%s  %s" % (GREEN, NC, name))
    PASSED.append(name)


def failed(name, detail=""):
    print("  %sFAIL%s  %s%s" % (RED, NC, name,
                                 "\n          " + detail.replace(
                                     "\n", "\n          ") if detail else ""))
    FAILED.append(name)


def skipped(name, why):
    print("  %sSKIP%s  %s (%s)" % (YELLOW, NC, name, why))
    SKIPPED.append(name)


def check(name, condition, detail=""):
    if condition:
        passed(name)
    else:
        failed(name, detail)
    return bool(condition)


def heading(text):
    print("\n%s%s%s" % (BOLD, text, NC))


def sh(*argv, stdin=None, check_code=True):
    done = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    if check_code and done.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(argv),
                                              done.stderr.strip()))
    return done


class Loops:
    """The loop devices of this run, and nothing else."""

    def __init__(self, directory):
        self.directory = directory
        self.devices = {}            # /dev/loopN -> backing file
        self.mapped = {}             # /dev/dm-N -> device-mapper name
        self.mounts = []
        self.count = 0

    def create(self, size, fill=None, sector=None):
        self.count += 1
        path = os.path.join(self.directory, "disk%d.img" % self.count)
        with open(path, "wb") as handle:
            if fill is None:
                handle.truncate(size)
            else:
                block = fill * MIB
                for _ in range(size // MIB):
                    handle.write(block)
        extra = ("--sector-size", str(sector)) if sector else ()
        device = sh("losetup", "--find", "--show", "--partscan", *extra,
                    path).stdout.strip()
        self.devices[device] = path
        sh("udevadm", "settle")
        return device

    def own(self, device):
        """Refuse to go on with anything that is not one of ours.

        The one guard that matters in a script run as root: a device path
        reaches the helper only if this run created it.
        """
        if device in self.mapped:
            return device
        match = re.match(r"^(/dev/loop[0-9]+)(p[0-9]+)?$", device)
        base = match.group(1) if match else None
        if base not in self.devices:
            raise SystemExit("Refusing to touch %s: not a loop device of "
                             "this run." % device)
        listed = sh("losetup", "--list", "--noheadings", "--output",
                    "BACK-FILE", base).stdout.strip()
        if listed != self.devices[base]:
            raise SystemExit("%s is no longer backed by %s." % (
                base, self.devices[base]))
        return device

    def mount(self, device, options=None, kind=None):
        point = tempfile.mkdtemp(prefix="mnt-", dir=self.directory)
        argv = ["mount"]
        if kind:
            argv += ["-t", kind]
        if options:
            argv += ["-o", options]
        sh(*argv, self.own(device), point)
        self.mounts.append(point)
        return point

    def unmount(self, point):
        # The desktop's indexer may be looking at a volume just mounted;
        # it lets go within a moment.
        import time
        for attempt in range(20):
            done = sh("umount", point, check_code=False)
            if done.returncode == 0:
                break
            if "busy" not in done.stderr or attempt == 19:
                raise RuntimeError("umount %s failed: %s"
                                   % (point, done.stderr.strip()))
            time.sleep(0.5)
        self.mounts.remove(point)
        os.rmdir(point)

    def detach(self, device):
        sh("losetup", "--detach", device)
        os.remove(self.devices.pop(device))

    def with_bad_block(self, device, sector, sectors=8):
        """A device-mapper view of a loop device of this run in which
        `sectors` sectors from `sector` on cannot be read: a drive with a
        bad patch, as far as anything reading it can tell."""
        self.own(device)
        size = int(sh("blockdev", "--getsz", device).stdout)
        name = "kss-disktest-%d-%d" % (os.getpid(), len(self.mapped))
        table = ("0 %d linear %s 0\n%d %d error\n%d %d linear %s %d\n"
                 % (sector, device, sector, sectors, sector + sectors,
                    size - sector - sectors, device, sector + sectors))
        sh("dmsetup", "create", name, stdin=table)
        sh("udevadm", "settle")
        node = os.path.realpath("/dev/mapper/" + name)
        self.mapped[node] = name
        return node

    def unmap(self, node):
        sh("dmsetup", "remove", self.mapped.pop(node))

    def cleanup(self):
        for point in list(self.mounts):
            subprocess.run(["umount", "--lazy", point],
                           stderr=subprocess.DEVNULL)
        for name in list(self.mapped.values()):
            if name:
                subprocess.run(["dmsetup", "remove", "--force", name],
                               stderr=subprocess.DEVNULL)
        for device in list(self.devices):
            subprocess.run(["losetup", "--detach", device],
                           stderr=subprocess.DEVNULL)
        shutil.rmtree(self.directory, ignore_errors=True)


def probe(device):
    """What blkid finds on the device, one tag at a time.

    Not its all-in-one output: that escapes what it prints, so a label with
    a space in it would not compare equal to itself.
    """
    found = {}
    for tag in ("TYPE", "LABEL", "UUID", "PTTYPE"):
        done = sh("blkid", "--probe", "--output", "value", "--match-tag", tag,
                  device, check_code=False)
        value = done.stdout.rstrip("\n")
        if done.returncode == 0 and value:
            found[tag] = value
    return found


def table(device):
    return json.loads(sh("sfdisk", "--json", device).stdout)["partitiontable"]


def first_partition(device):
    return device + "p1"


FSCK = {
    "ext4": ["e2fsck", "-f", "-n"],
    "vfat": ["fsck.fat", "-n"],
    "exfat": ["fsck.exfat", "-n"],
    "btrfs": ["btrfs", "check", "--readonly"],
    "xfs": ["xfs_repair", "-n"],
    "f2fs": ["fsck.f2fs", "--dry-run"],
    "ntfs": ["ntfsfix", "--no-action"],
}


def fsck_clean(fstype, device):
    argv = FSCK[fstype]
    if shutil.which(argv[0]) is None:
        return None
    return sh(*argv, device, check_code=False).returncode == 0


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def request_format(device, fstype, label="", table_type=None, erase="none",
                   owner=False, uuid=None, cluster=None):
    name = os.path.basename(device)
    return {"op": "format", "device": device,
            "identity": disks.identity(name), "fstype": fstype,
            "label": label, "erase": erase, "table": table_type,
            "owner": owner, "uuid": uuid, "cluster": cluster}


def boot_cluster(device):
    """The cluster size the boot sector on `device` declares, read here
    independently of the helper's own reading."""
    with open(device, "rb") as handle:
        boot = handle.read(512)
    if boot[3:11] == b"NTFS    ":
        per = boot[0x0D]
        return int.from_bytes(boot[11:13], "little") * (
            per if per <= 0x80 else 1 << (256 - per))
    if boot[3:11] == b"EXFAT   ":
        return 1 << (boot[108] + boot[109])
    return int.from_bytes(boot[11:13], "little") * boot[13]


def installed(fs):
    return diskfs.find_tool(*fs.mkfs) is not None


def main():
    if os.geteuid() != 0:
        print("This check creates loop devices and formats them, so it needs "
              "root:\n    sudo %s" % os.path.abspath(__file__))
        return 2
    for tool in ("losetup", "sfdisk", "blkid", "wipefs", "udevadm"):
        if shutil.which(tool) is None:
            print("Missing '%s' (util-linux)." % tool)
            return 2

    caller = None
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        if os.environ.get(variable, "").isdigit():
            caller = int(os.environ[variable])
            break

    base = os.environ.get("TMPDIR") or "/var/tmp"
    loops = Loops(tempfile.mkdtemp(prefix="ksyssupervisor-disktest-",
                                   dir=base))
    client = diskctl.DiskHelperClient(
        path=HELPER, launcher=lambda path: [sys.executable, path])
    print("Helper under test: %s" % HELPER)
    print("Working directory: %s" % loops.directory)
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    unknown = [name for name in only if name not in SECTIONS]
    if unknown:
        print("Unknown section %s. There are: %s"
              % (", ".join(unknown), ", ".join(SECTIONS)))
        loops.cleanup()
        return 2
    try:
        client.start()
        for name, section in SECTIONS.items():
            if not only or name in only:
                section(loops, client, caller)
    finally:
        client.stop()
        loops.cleanup()

    print("\n%s%d passed, %d failed, %d skipped%s"
          % (BOLD, len(PASSED), len(FAILED), len(SKIPPED), NC))
    for name in FAILED:
        print("  %sfailed:%s %s" % (RED, NC, name))
    return 1 if FAILED else 0


def runner(loops, client):
    """Send a request to the helper - for a device of this run only."""
    def do(request, on_event=None):
        for key in ("device", "source", "target"):
            if isinstance(request.get(key), str) \
                    and request[key].startswith("/dev/"):
                loops.own(request[key])
        return client.run(request, on_event=on_event)
    return do


def check_format(loops, client, caller):
    do = runner(loops, client)

    # ---- every filesystem, on a whole drive ------------------------------
    heading("Formatting a whole drive")
    for index, fs in enumerate(diskfs.FILESYSTEMS.values()):
        name = "%s on a whole drive" % fs.title
        if not installed(fs):
            skipped(name, "%s is not installed" % fs.mkfs[0])
            continue
        size = max(fs.min_size * 2, 512 * MIB)
        device = loops.create(size)
        table_type = "gpt" if index % 2 == 0 else "dos"
        label = ("TESTVOL" if fs.label_counts == "dos"
                 else "Test vol" if fs.label_spaces else "Test-vol")
        try:
            result = do(request_format(device, fs.key, label, table_type,
                                       owner=True))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
            loops.detach(device)
            continue
        part = first_partition(device)
        problems = []
        if result.get("device") != part:
            problems.append("helper reported %r" % result.get("device"))
        found = probe(part)
        if found.get("TYPE") != fs.key:
            problems.append("blkid says %r" % found.get("TYPE"))
        if found.get("LABEL") != label:
            problems.append("label is %r" % found.get("LABEL"))
        layout = table(device)
        if layout["label"] != table_type:
            problems.append("table is %s" % layout["label"])
        partitions = layout.get("partitions", [])
        if len(partitions) != 1:
            problems.append("%d partitions" % len(partitions))
        else:
            start = partitions[0]["start"] * layout.get("sectorsize", 512)
            if start != MIB:
                problems.append("partition starts at %d" % start)
            end = start + partitions[0]["size"] * layout.get("sectorsize", 512)
            if size - end > 2 * MIB:
                problems.append("%d bytes left unused at the end"
                                % (size - end))
        clean = fsck_clean(fs.key, part)
        if clean is False:
            problems.append("fsck reports errors")
        try:
            point = loops.mount(part)
        except RuntimeError as exc:
            # A kernel built without this filesystem cannot mount it; that
            # says nothing about whether it was created properly.
            if "unknown filesystem type" not in str(exc):
                problems.append("does not mount: %s" % exc)
        else:
            if fs.native and caller is not None:
                owner = os.stat(point).st_uid
                if owner != caller:
                    problems.append("root directory owned by uid %d, not "
                                    "%d" % (owner, caller))
            with open(os.path.join(point, "hello.txt"), "w") as handle:
                handle.write("written after formatting\n")
            loops.unmount(point)
        check("%s, %s table" % (name, table_type.upper()), not problems,
              "; ".join(problems))
        loops.detach(device)

    # ---- the smallest size each filesystem is offered at -----------------
    heading("Minimum sizes")
    for fs in diskfs.FILESYSTEMS.values():
        if not installed(fs):
            continue
        device = loops.create(fs.min_size + 2 * MIB)
        try:
            do(request_format(device, fs.key, "", "gpt"))
            check("%s at its minimum of %s" % (
                fs.title, disks.format_size(fs.min_size)),
                probe(first_partition(device)).get("TYPE") == fs.key)
        except diskctl.HelperError as exc:
            failed("%s at its minimum of %s" % (
                fs.title, disks.format_size(fs.min_size)), str(exc))
        loops.detach(device)

        device = loops.create(max(fs.min_size // 2, 4 * MIB))
        before = digest(loops.devices[device])
        try:
            do(request_format(device, fs.key, "", "gpt"))
            failed("%s below its minimum is refused" % fs.title,
                   "it was formatted")
        except diskctl.HelperError as exc:
            check("%s below its minimum is refused untouched" % fs.title,
                  "too small" in str(exc)
                  and digest(loops.devices[device]) == before, str(exc))
        loops.detach(device)

    # ---- cluster sizes -----------------------------------------------------
    heading("Cluster sizes")
    cases = [("exfat", GIB, 512, (512, 4096, 32768, MIB, 2 * MIB)),
             ("vfat", GIB, 512, (512, 4096, 8192)),
             ("vfat", 5 * GIB, 512, (65536,)),
             ("ntfs", GIB, 512, (512, 4096, 65536, 2 * MIB)),
             # mkfs.fat counts in sectors: on 4 KiB sectors 8 KiB is 2.
             ("vfat", 2 * GIB, 4096, (4096, 8192)),
             ("exfat", GIB, 4096, (4096, 131072)),
             ("ntfs", GIB, 4096, (4096, 65536))]
    for fstype, size, sector, clusters in cases:
        fs = diskfs.FILESYSTEMS[fstype]
        if not installed(fs):
            skipped("%s cluster sizes" % fs.title,
                    "%s is not installed" % fs.mkfs[0])
            continue
        for cluster in clusters:
            name = "%s, %s, %d-byte sectors: %s clusters" % (
                fs.title, disks.format_size(size), sector,
                diskfs.format_cluster(cluster))
            if not diskfs.cluster_valid(fstype, size - 2 * MIB, sector,
                                        cluster):
                failed(name, "the dialog would not offer it")
                continue
            device = loops.create(size, sector=sector)
            try:
                do(request_format(device, fstype, "", "gpt",
                                  cluster=cluster))
            except diskctl.HelperError as exc:
                failed(name, str(exc))
                loops.detach(device)
                continue
            part = first_partition(device)
            problems = []
            found = boot_cluster(part)
            if found != cluster:
                problems.append("boot sector says %d" % found)
            if fsck_clean(fstype, part) is False:
                problems.append("fsck reports errors")
            # Each driver that may mount it: the kernel's own, and for
            # NTFS ntfs-3g as well.
            kinds = [None] + (["ntfs-3g"] if fstype == "ntfs"
                              and shutil.which("ntfs-3g") else [])
            for kind in kinds:
                try:
                    point = loops.mount(part, kind=kind)
                except RuntimeError as exc:
                    problems.append("%s does not mount it: %s"
                                    % (kind or "the kernel", exc))
                    continue
                block = os.statvfs(point).f_bsize
                if block != cluster:
                    problems.append("%s reports %d-byte blocks"
                                    % (kind or "the kernel", block))
                with open(os.path.join(point, "small.txt"), "w") as handle:
                    handle.write("x" * 100)
                loops.unmount(point)
            if fsck_clean(fstype, part) is False:
                problems.append("fsck reports errors after writing")
            check(name, not problems, "; ".join(problems))
            loops.detach(device)

    if installed(diskfs.FILESYSTEMS["exfat"]):
        device = loops.create(GIB)
        before = digest(loops.devices[device])
        try:
            do(request_format(device, "exfat", "", "gpt", cluster=64 * MIB))
            failed("a cluster size the volume cannot have is refused",
                   "it was formatted")
        except diskctl.HelperError as exc:
            check("a cluster size the volume cannot have is refused "
                  "untouched", "cannot have clusters" in str(exc)
                  and digest(loops.devices[device]) == before, str(exc))
        loops.detach(device)

    # ---- one partition among others --------------------------------------
    heading("Formatting one partition, leaving its neighbour alone")
    device = loops.create(1024 * MIB)
    sh("sfdisk", "--quiet", device,
       stdin="label: gpt\nsize=400MiB, type=L\ntype=L\n")
    sh("udevadm", "settle")
    first, second = device + "p1", device + "p2"
    sh("mkfs.ext4", "-q", "-F", "-L", "keepme", loops.own(first))
    point = loops.mount(first)
    with open(os.path.join(point, "precious.bin"), "wb") as handle:
        handle.write(os.urandom(8 * MIB))
    sh("sync")
    precious = digest(os.path.join(point, "precious.bin"))

    for fs in diskfs.FILESYSTEMS.values():
        if not installed(fs) or fs.min_size > 600 * MIB:
            continue
        name = "partition 2 as %s" % fs.title
        try:
            do(request_format(second, fs.key, "SECOND"))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
            continue
        problems = []
        if probe(second).get("TYPE") != fs.key:
            problems.append("blkid says %r" % probe(second).get("TYPE"))
        if digest(os.path.join(point, "precious.bin")) != precious:
            problems.append("THE NEIGHBOUR'S FILE CHANGED")
        if not os.path.ismount(point):
            problems.append("the neighbour was unmounted")
        kind = table(device)["partitions"][1]["type"]
        want = ("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7" if not fs.native
                else "0FC63DAF-8483-4772-8E79-3D69D8477DE4")
        if kind.upper() != want:
            problems.append("partition type is %s" % kind)
        check(name, not problems, "; ".join(problems))

    # ---- a volume in use ---------------------------------------------------
    heading("Refusals")
    held = open(os.path.join(point, "precious.bin"), "rb")
    try:
        do(request_format(first, "ext4", "x"))
        failed("a volume with an open file is refused", "it was formatted")
    except diskctl.HelperError as exc:
        held.close()
        # Refused for the right reason: because it is busy, not because it
        # was mistaken for something else.
        check("a volume with an open file is refused as busy, data intact",
              "busy" in str(exc)
              and digest(os.path.join(point, "precious.bin")) == precious,
              str(exc))
    finally:
        held.close()

    stale = request_format(second, "ext4", "x")
    stale["identity"]["size"] += 512
    try:
        do(stale)
        failed("a device whose size changed is refused", "it was formatted")
    except diskctl.HelperError as exc:
        check("a device whose size changed is refused",
              "not the device" in str(exc), str(exc))

    name = "a whole drive with a mounted, idle volume: unmounted, then formatted"
    try:
        do(request_format(device, "ext4", "x", "gpt"))
    except diskctl.HelperError as exc:
        failed(name, str(exc))
    else:
        check(name, probe(first).get("TYPE") == "ext4"
              and not os.path.ismount(point))
        loops.mounts.remove(point)
        os.rmdir(point)

    # Same loop number, different backing file: the name is the same and the
    # kernel's sequence number is not.
    remembered = request_format(device, "ext4", "x", "gpt")
    number = device
    loops.detach(device)
    replacement = loops.create(1024 * MIB)
    if replacement == number and remembered["identity"]["diskseq"] is not None:
        try:
            do(remembered)
            failed("a swapped device under the same name is refused",
                   "it was formatted")
        except diskctl.HelperError as exc:
            check("a swapped device under the same name is refused",
                  "unplugged" in str(exc), str(exc))
    else:
        skipped("a swapped device under the same name",
                "the kernel gave the new device another name")
    device = replacement

    # ---- keeping an ID -------------------------------------------------------
    heading("Reformatting with the same ID")
    do(request_format(device, "ext4", "one", "gpt"))
    part = first_partition(device)
    uuid = probe(part)["UUID"]
    do(request_format(part, "ext4", "two", uuid=uuid))
    check("ext4 keeps its UUID across a reformat",
          probe(part).get("UUID") == uuid and probe(part).get("LABEL") == "two")
    if installed(diskfs.FILESYSTEMS["vfat"]):
        do(request_format(part, "vfat", "FAT"))
        serial = probe(part)["UUID"]
        do(request_format(part, "vfat", "FAT2", uuid=serial))
        check("FAT keeps its volume ID across a reformat",
              probe(part).get("UUID") == serial)

    # ---- renaming --------------------------------------------------------------
    heading("Renaming")
    for fs in diskfs.FILESYSTEMS.values():
        if not installed(fs) or fs.min_size > 900 * MIB:
            continue
        tool = {"ext4": "e2label", "vfat": "fatlabel", "exfat": "exfatlabel",
                "ntfs": "ntfslabel", "btrfs": "btrfs",
                "xfs": "xfs_admin"}.get(fs.key)
        if tool is None or diskfs.find_tool(tool) is None:
            skipped("renaming %s" % fs.title, "no tool for it here")
            continue
        do(request_format(part, fs.key, "BEFORE"))
        new = ("AFTER" if fs.label_counts == "dos"
               else "After it" if fs.label_spaces else "After-it")
        try:
            do({"op": "label", "device": part,
                "identity": disks.identity(os.path.basename(part)),
                "label": new})
            check("renaming %s" % fs.title, probe(part).get("LABEL") == new,
                  "label is %r" % probe(part).get("LABEL"))
        except diskctl.HelperError as exc:
            failed("renaming %s" % fs.title, str(exc))
    loops.detach(device)

    # ---- full format ---------------------------------------------------------
    heading("Full format")
    device = loops.create(96 * MIB, fill=b"\xa5")
    sh("sfdisk", "--quiet", device, stdin="label: gpt\ntype=L\n")
    sh("udevadm", "settle")
    part = first_partition(device)
    steps = []
    do(request_format(part, "ext4", "zeroed", erase="zero"),
       on_event=steps.append)
    sh("sync")
    with open(part, "rb") as handle:
        data = handle.read()
    check("overwriting leaves none of the old contents",
          b"\xa5" * 4096 not in data and probe(part).get("TYPE") == "ext4")
    # The overwrite's progress, in bytes; mkfs afterwards counts its own
    # phases in items ("unit": "items").
    progress = [e for e in steps if e.get("event") == "progress"
                and not e.get("unit")]
    check("progress runs to the end of the device",
          progress and progress[-1]["done"] == progress[-1]["total"]
          == len(data))
    loops.detach(device)

    device = loops.create(96 * MIB, fill=b"\xa5")
    try:
        do(request_format(device, "ext4", "trimmed", "gpt", erase="discard"))
        with open(first_partition(device), "rb") as handle:
            data = handle.read()
        check("discarding leaves none of the old contents",
              b"\xa5" * 4096 not in data)
    except diskctl.HelperError as exc:
        skipped("discard", str(exc).splitlines()[-1])
    loops.detach(device)

    # ---- stopping --------------------------------------------------------------
    heading("Stopping")
    # Large enough that the overwrite cannot finish before the stop arrives;
    # sparse, so only what is written before the stop takes any room.
    device = loops.create(8192 * MIB)
    sh("sfdisk", "--quiet", device, stdin="label: gpt\ntype=L\n")
    sh("udevadm", "settle")
    part = first_partition(device)
    sh("mkfs.ext4", "-q", "-F", loops.own(part))

    def stop_on_progress(event):
        if event.get("event") == "progress":
            client.cancel()

    try:
        do(request_format(part, "ext4", "never", erase="zero"),
           on_event=stop_on_progress)
        failed("a full format can be stopped", "it ran to the end")
    except diskctl.Cancelled as exc:
        check("a full format can be stopped, and says what was lost",
              "no usable filesystem" in str(exc)
              and probe(part).get("TYPE") is None, str(exc))
    except diskctl.HelperError as exc:
        failed("a full format can be stopped", str(exc))
    do(request_format(part, "ext4", "again"))
    check("the helper carries on after a stop",
          probe(part).get("LABEL") == "again")

    # ---- the client disappearing ---------------------------------------------
    heading("The window closing mid-operation")
    lone = diskctl.DiskHelperClient(
        path=HELPER, launcher=lambda path: [sys.executable, path])
    client.stop()
    lone.start()
    request = request_format(device, "ext4", "survivor", "gpt")
    loops.own(device)
    lone._send(dict(request, cmd="run", id=99))
    proc = lone._proc
    proc.stdin.close()               # the window is gone
    proc.wait(timeout=120)
    lone._reap()
    sh("udevadm", "settle")
    check("a format in progress is finished, not abandoned halfway",
          probe(first_partition(device)).get("LABEL") == "survivor"
          and fsck_clean("ext4", first_partition(device)))
    client.start()
    loops.detach(device)


def check_restore(loops, client, caller):
    """A drive that is not one piece, put back together."""
    do = runner(loops, client)
    heading("Restoring a drive to one volume")
    size = 600 * MIB

    def one_volume(name, device, fstype, table_type):
        try:
            do(request_format(device, fstype, "WHOLE", table_type))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
            return
        sh("udevadm", "settle")
        problems = []
        bare = probe(device)
        if bare.get("TYPE"):
            problems.append("the bare drive still reads as %s" % bare["TYPE"])
        if bare.get("PTTYPE") != table_type:
            problems.append("table reads as %r" % bare.get("PTTYPE"))
        layout = table(device)
        partitions = layout.get("partitions", [])
        if len(partitions) != 1:
            problems.append("%d partitions" % len(partitions))
        else:
            unit = layout.get("sectorsize", 512)
            end = (partitions[0]["start"] + partitions[0]["size"]) * unit
            if size - end > 2 * MIB:
                problems.append("%d bytes left unused" % (size - end))
        part = first_partition(device)
        if probe(part).get("TYPE") != fstype:
            problems.append("no %s on the partition" % fstype)
        if os.path.exists(device + "p2"):
            problems.append("an old second partition is still listed")
        listed = json.loads(sh("lsblk", "--json", "--output",
                               "NAME,FSTYPE", device).stdout)
        if listed["blockdevices"][0].get("fstype"):
            problems.append("lsblk still shows %s on the drive"
                            % listed["blockdevices"][0]["fstype"])
        check(name, not problems, "; ".join(problems))

    # Two partitions, as a Windows installer stick has them.
    device = loops.create(size)
    sh("sfdisk", "--quiet", device,
       stdin="label: gpt\nsize=560MiB, type=EBD0A0A2-B9E5-4433-87C0-"
             "68B6B72699C7\ntype=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n")
    sh("udevadm", "settle")
    if shutil.which("mkfs.exfat") and shutil.which("mkfs.fat"):
        sh("mkfs.exfat", loops.own(device + "p1"))
        sh("mkfs.fat", "-n", "BOOT", loops.own(device + "p2"))
        one_volume("two partitions become one, GPT", device, "exfat", "gpt")
    else:
        skipped("two partitions become one", "mkfs.exfat or mkfs.fat missing")
    loops.detach(device)

    # An installer image: a disc filesystem over the whole device with a
    # partition table of its own inside it.
    if shutil.which("xorriso") is None or shutil.which("mkfs.fat") is None:
        skipped("an installer image is removed", "xorriso is not installed")
        return
    work = tempfile.mkdtemp(prefix="iso-", dir=loops.directory)
    tree = os.path.join(work, "tree")
    os.mkdir(tree)
    with open(os.path.join(tree, "payload.bin"), "wb") as handle:
        handle.write(os.urandom(8 * MIB))
    boot = os.path.join(work, "efi.img")
    with open(boot, "wb") as handle:
        handle.truncate(4 * MIB)
    sh("mkfs.fat", "-n", "ANACONDA", boot)
    image = os.path.join(work, "live.iso")
    sh("xorriso", "-as", "mkisofs", "-o", image, "-V", "Live-Image",
       "-partition_offset", "16", "-append_partition", "2", "0xef", boot,
       tree)
    for table_type in ("dos", "gpt"):
        device = loops.create(size)
        with open(image, "rb") as source, \
                open(loops.own(device), "r+b") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        sh("partprobe", device, check_code=False)
        sh("udevadm", "settle")
        before = probe(device)
        if before.get("TYPE") != "iso9660":
            skipped("an installer image is removed, %s"
                    % table_type.upper(),
                    "the test image did not come out as one: %r" % before)
        else:
            one_volume("an installer image is removed, %s table"
                       % table_type.upper(), device, "exfat"
                       if shutil.which("mkfs.exfat") else "vfat", table_type)
        loops.detach(device)
    shutil.rmtree(work, ignore_errors=True)


def ask(op, device, **fields):
    request = {"op": op, "device": device,
               "identity": disks.identity(os.path.basename(device))}
    request.update(fields)
    return request


def made(loops, size, fstype, fill=None, label=""):
    """A loop device holding one filesystem straight on it, or None when
    the tool that makes it is missing."""
    fs = diskfs.FILESYSTEMS[fstype]
    tool = diskfs.find_tool(*fs.mkfs)
    if tool is None:
        return None
    device = loops.create(size, fill=fill)
    quiet = {"ext4": ["-q", "-F"], "xfs": ["-q", "-f"], "btrfs": ["-q", "-f"],
             "ntfs": ["--quick", "--force"], "f2fs": ["-q", "-f"]}
    sh(tool, *quiet.get(fstype, []), loops.own(device))
    sh("udevadm", "settle")
    return device


def pattern_blocks(path, byte, block=4096):
    """How many whole blocks of the file or device are nothing but `byte`."""
    want = byte * block
    count = 0
    with open(path, "rb") as handle:
        while True:
            data = handle.read(block * 256)
            if not data:
                break
            for offset in range(0, len(data) - block + 1, block):
                if data[offset:offset + block] == want:
                    count += 1
    return count


def check_maintain(loops, client, caller):
    do = runner(loops, client)

    # ---- TRIM ------------------------------------------------------------
    heading("TRIM")
    device = made(loops, 300 * MIB, "ext4")
    point = loops.mount(device)
    try:
        result = do(ask("trim", device))
        check("a mounted filesystem is trimmed and the amount comes back",
              isinstance(result.get("trimmed"), int)
              and result["trimmed"] > 0, repr(result))
    except diskctl.HelperError as exc:
        failed("a mounted filesystem is trimmed", str(exc))
    loops.unmount(point)
    try:
        do(ask("trim", device))
        failed("an unmounted volume is not trimmed", "it was accepted")
    except diskctl.HelperError as exc:
        check("an unmounted volume says to mount it first",
              "not mounted" in str(exc), str(exc))
    loops.detach(device)

    state = sh("systemctl", "is-enabled", "fstrim.timer",
               check_code=False).stdout.strip()
    if state == "enabled":
        # Asked for what is already so: proves the command and the reading
        # back without changing this machine's setting.
        try:
            result = client.run({"op": "trim_timer", "enable": True})
            check("the weekly TRIM setting is read back",
                  result == {"enabled": True}, repr(result))
        except diskctl.HelperError as exc:
            failed("the weekly TRIM setting is read back", str(exc))
    else:
        skipped("the weekly TRIM setting", "fstrim.timer is %s here; not "
                "changing it for a test" % (state or "absent"))

    # ---- fragmentation -----------------------------------------------------
    heading("Fragmentation")
    for fstype, size in (("ext4", 300 * MIB), ("xfs", 400 * MIB),
                         ("btrfs", 400 * MIB)):
        device = made(loops, size, fstype)
        if device is None:
            skipped("%s fragmentation" % fstype, "mkfs is missing")
            continue
        point = loops.mount(device)
        # Two files grown in turns, so their blocks interleave.
        with open(os.path.join(point, "a"), "ab") as first, \
                open(os.path.join(point, "b"), "ab") as second:
            for _ in range(200):
                for handle in (first, second):
                    handle.write(os.urandom(64 * 1024))
                    handle.flush()
                    os.fsync(handle.fileno())
        before = digest(os.path.join(point, "a"))
        if fstype != "btrfs":
            try:
                result = do(ask("defrag_analyse", device))
                good = (result["verdict"] in ("none", "some", "needed")
                        and isinstance(result["score"], int)) \
                    if fstype == "ext4" else (
                        result["verdict"] == "unrated"
                        and isinstance(result["factor"], float))
                check("%s fragmentation is measured (%s)" % (
                    fstype, {k: v for k, v in result.items()
                             if k not in ("device", "fstype")}), good)
            except diskctl.HelperError as exc:
                failed("%s fragmentation is measured" % fstype, str(exc))
        try:
            result = do(ask("defrag", device))
            check("%s is defragmented and the files are unchanged" % fstype,
                  digest(os.path.join(point, "a")) == before, repr(result))
        except diskctl.HelperError as exc:
            failed("%s is defragmented" % fstype, str(exc))
        loops.unmount(point)
        clean = fsck_clean(fstype, device)
        check("%s is consistent after defragmenting" % fstype,
              clean is not False)
        loops.detach(device)

    device = made(loops, 200 * MIB, "ntfs")
    if device is not None:
        try:
            do(ask("defrag", device))
            failed("NTFS is not defragmented", "it was accepted")
        except diskctl.HelperError as exc:
            check("NTFS is refused for defragmenting, with the reason",
                  "cannot be defragmented" in str(exc), str(exc))
        loops.detach(device)

    # ---- free space --------------------------------------------------------
    heading("Overwriting free space")
    marker = b"\xaa"
    for fstype, size, most_left in (
            ("ext4", 256 * MIB, 0.15), ("vfat", 256 * MIB, 0.05),
            ("exfat", 256 * MIB, 0.05), ("ntfs", 256 * MIB, 0.10),
            ("xfs", 400 * MIB, 0.25), ("btrfs", 600 * MIB, 0.45)):
        name = "%s free space is overwritten, files are not" % fstype
        # A device full of a marker byte: whatever the filesystem does not
        # write itself still shows it, so what the wipe reached is countable.
        device = made(loops, size, fstype, fill=marker)
        if device is None:
            skipped(name, "mkfs is missing")
            continue
        backing = loops.devices[device]
        try:
            point = loops.mount(device)
        except RuntimeError as exc:
            skipped(name, "cannot be mounted here: %s"
                    % str(exc).splitlines()[-1])
            loops.detach(device)
            continue
        kept = os.path.join(point, "keep.bin")
        with open(kept, "wb") as handle:
            handle.write(os.urandom(5 * MIB))
        sh("sync")
        before_digest = digest(kept)
        free_before = os.statvfs(point).f_bavail
        marked_before = pattern_blocks(backing, marker)
        problems = []
        try:
            result = do(ask("wipe_free", device, pattern="zero"))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
            loops.unmount(point)
            loops.detach(device)
            continue
        if digest(kept) != before_digest:
            problems.append("the existing file changed")
        left = [n for n in os.listdir(point)
                if n.startswith(".ksyssupervisor")]
        if left:
            problems.append("left behind: %s" % left)
        free_after = os.statvfs(point).f_bavail
        if free_after < free_before * 0.97:
            problems.append("free space went from %d to %d blocks"
                            % (free_before, free_after))
        if fstype == "btrfs" and result.get("pattern") != "random":
            problems.append("btrfs was not given random data")
        loops.unmount(point)
        marked_after = pattern_blocks(backing, marker)
        share = marked_after / max(1, marked_before)
        if share > most_left:
            problems.append("%d of %d marked blocks were never written"
                            % (marked_after, marked_before))
        if fsck_clean(fstype, device) is False:
            problems.append("fsck reports errors afterwards")
        check("%s (%.1f%% of the old blocks left, %s written)" % (
            name, share * 100, disks.format_size(result.get("written", 0))),
            not problems, "; ".join(problems))
        loops.detach(device)

    device = made(loops, 4096 * MIB, "ext4")
    point = loops.mount(device)

    def stop_on_progress(event):
        if event.get("event") == "progress":
            client.cancel()

    try:
        do(ask("wipe_free", device, pattern="random"),
           on_event=stop_on_progress)
        failed("overwriting free space can be stopped", "it ran to the end")
    except diskctl.Cancelled:
        left = [n for n in os.listdir(point)
                if n.startswith(".ksyssupervisor")]
        check("stopping removes the files used for filling", not left,
              repr(left))
    except diskctl.HelperError as exc:
        failed("overwriting free space can be stopped", str(exc))

    stale = os.path.join(point, ".ksyssupervisor-wipe-999999")
    os.mkdir(stale)
    with open(os.path.join(stale, "000001"), "wb") as handle:
        handle.write(b"\0" * MIB)
    try:
        result = do(ask("wipe_cleanup", device))
        check("files left by an interrupted run are found and removed",
              result.get("removed") == 1 and not os.path.exists(stale),
              repr(result))
    except diskctl.HelperError as exc:
        failed("files left by an interrupted run are removed", str(exc))
    loops.unmount(point)
    loops.detach(device)

    # ---- checking ----------------------------------------------------------
    heading("Checking and repairing")
    for fstype in ("ext4", "vfat", "exfat", "ntfs", "xfs", "btrfs", "f2fs"):
        size = max(diskfs.FILESYSTEMS[fstype].min_size * 2, 300 * MIB)
        device = made(loops, size, fstype)
        if device is None:
            skipped("a clean %s checks as clean" % fstype, "mkfs is missing")
            continue
        try:
            result = do(ask("check", device))
            check("a clean %s checks as clean" % fstype,
                  result.get("state") == "clean",
                  result.get("output", "")[-400:])
        except diskctl.HelperError as exc:
            failed("a clean %s checks as clean" % fstype, str(exc))
        loops.detach(device)

    # ext4 with a directory entry pointing at a cleared inode.
    device = made(loops, 300 * MIB, "ext4")
    point = loops.mount(device)
    with open(os.path.join(point, "victim"), "w") as handle:
        handle.write("x" * 100000)
    with open(os.path.join(point, "bystander"), "w") as handle:
        handle.write("still here")
    loops.unmount(point)
    sh("debugfs", "-w", "-R", "clri /victim", loops.own(device),
       check_code=False)
    try:
        result = do(ask("check", device))
        check("damage to ext4 is found, and nothing is changed by looking",
              result.get("state") == "errors"
              and fsck_clean("ext4", device) is False, repr(result)[:300])
        result = do(ask("check", device, repair=True))
        point = loops.mount(device)
        with open(os.path.join(point, "bystander")) as handle:
            intact = handle.read() == "still here"
        loops.unmount(point)
        check("ext4 is repaired, and checked again afterwards",
              result.get("state") == "repaired"
              and fsck_clean("ext4", device) and intact, repr(result)[:300])
    except diskctl.HelperError as exc:
        failed("ext4 damage and repair", str(exc))
    loops.detach(device)

    # FAT whose two allocation tables disagree.
    device = made(loops, 300 * MIB, "vfat")
    if device is not None:
        with open(loops.own(device), "r+b") as handle:
            boot = handle.read(512)
            reserved = int.from_bytes(boot[14:16], "little")
            # FAT12/16 keep the table's size in one field, FAT32 in another.
            per_fat = int.from_bytes(boot[22:24], "little") or \
                int.from_bytes(boot[36:40], "little")
            second = (reserved + per_fat) * 512
            handle.seek(second + 64)
            handle.write(b"\xf8\xff\xff\x0f" * 4)
        sh("sync")
        try:
            result = do(ask("check", device))
            check("damage to FAT is found", result.get("state") == "errors",
                  repr(result)[:300])
            result = do(ask("check", device, repair=True))
            check("FAT is repaired, and checked again afterwards",
                  result.get("state") == "repaired"
                  and fsck_clean("vfat", device), repr(result)[:300])
        except diskctl.HelperError as exc:
            failed("FAT32 damage and repair", str(exc))
        loops.detach(device)

    device = made(loops, 300 * MIB, "ext4")
    point = loops.mount(device)
    try:
        result = do(ask("check", device))
        mounts = sh("findmnt", "--noheadings", "--source", device,
                    check_code=False).stdout.strip()
        check("a mounted volume is unmounted for its check, and that is "
              "reported", result.get("was_mounted") is True and not mounts,
              repr(result)[:200])
        loops.mounts.remove(point)
        os.rmdir(point)
    except diskctl.HelperError as exc:
        failed("a mounted volume is checked", str(exc))
        loops.unmount(point)
    loops.detach(device)

    # A Windows volume left hibernated.
    device = made(loops, 300 * MIB, "ntfs")
    if device is None or shutil.which("ntfs-3g.probe") is None:
        skipped("a hibernated Windows volume", "ntfs-3g is not installed")
    else:
        try:
            point = loops.mount(device)
            with open(os.path.join(point, "hiberfil.sys"), "wb") as handle:
                handle.write(b"hibr" + b"\0" * 8188)
            loops.unmount(point)
            code = sh("ntfs-3g.probe", "--readwrite", device,
                      check_code=False).returncode
            if code != 14:
                skipped("a hibernated Windows volume",
                        "could not make one: the probe says %d" % code)
            else:
                try:
                    do(ask("check", device, repair=True))
                    failed("a hibernated Windows volume is not repaired",
                           "the repair was accepted")
                except diskctl.HelperError as exc:
                    check("a hibernated Windows volume is not repaired, "
                          "and the user is told what to do in Windows",
                          "fast startup" in str(exc) and "Shift" in str(exc),
                          str(exc))
                result = do(ask("check", device))
                check("it can still be looked at", "state" in result)
        except (RuntimeError, diskctl.HelperError) as exc:
            failed("a hibernated Windows volume", str(exc))
    if device is not None:
        loops.detach(device)


def sha(path, length=None):
    found = hashlib.sha256()
    left = length
    with open(path, "rb") as handle:
        while left is None or left > 0:
            data = handle.read(MIB if left is None else min(MIB, left))
            if not data:
                break
            found.update(data)
            if left is not None:
                left -= len(data)
    return found.hexdigest()


def clone_request(source, target, mode="exact", **fields):
    request = {"op": "clone", "mode": mode,
               "source": source, "target": target,
               "source_identity": disks.identity(os.path.basename(source)),
               "target_identity": disks.identity(os.path.basename(target))}
    request.update(fields)
    return request


def populate(loops, device, name="data.bin", size=3 * MIB, kind=None):
    """Put a file of random data on a filesystem; returns its digest."""
    point = loops.mount(device, kind=kind)
    path = os.path.join(point, name)
    with open(path, "wb") as handle:
        handle.write(os.urandom(size))
    found = digest(path)
    loops.unmount(point)
    return found


def file_digest(loops, device, name="data.bin", kind=None):
    point = loops.mount(device, "ro", kind)
    try:
        return digest(os.path.join(point, name))
    except OSError:
        return None
    finally:
        loops.unmount(point)


def partitions_of(device):
    return [(p["start"], p["size"], p.get("type"), p.get("uuid"))
            for p in table(device).get("partitions", [])]


def gpt_sound(device):
    """Whether the GPT's two copies are where they belong and agree."""
    if shutil.which("sgdisk") is None:
        return None
    out = sh("sgdisk", "--verify", device, check_code=False)
    return "No problems found" in out.stdout


def check_copy(loops, client, caller):
    do = runner(loops, client)
    work = tempfile.mkdtemp(prefix="img-", dir=loops.directory)
    if caller is not None:
        # Image files are created as the user who asked, who therefore has
        # to be able to reach the folder.
        os.chmod(loops.directory, 0o711)
        os.chown(work, caller, -1)

    # ---- a whole drive, every block ---------------------------------------
    heading("Cloning a drive, every block")
    source = loops.create(300 * MIB)
    with open(loops.own(source), "r+b") as handle:
        handle.write(os.urandom(300 * MIB))        # no block is blank
    sh("sfdisk", "--quiet", source,
       stdin="label: gpt\nsize=120MiB, type=L\nsize=100MiB, type=EBD0A0A2-"
             "B9E5-4433-87C0-68B6B72699C7\n")
    sh("udevadm", "settle")
    sh("mkfs.ext4", "-q", "-F", "-L", "Root", loops.own(source + "p1"))
    sh("mkfs.fat", "-n", "DATA", loops.own(source + "p2"))
    kept = populate(loops, source + "p1")
    sh("sync")
    target = loops.create(420 * MIB, fill=b"\xaa")
    try:
        result = do(clone_request(source, target))
        sh("udevadm", "settle")
        problems = []
        # Between the two copies of the partition table, which are redone
        # for the larger drive, every byte is the original's.
        with open(source, "rb") as a, open(target, "rb") as b:
            for handle in (a, b):
                handle.seek(MIB)
            if a.read(298 * MIB) != b.read(298 * MIB):
                problems.append("the copy differs from the original")
        if partitions_of(source) != partitions_of(target):
            problems.append("the partition tables differ")
        if gpt_sound(target) is False:
            problems.append("the copy's GPT is not sound on the larger "
                            "drive")
        if not os.path.exists(target + "p2"):
            problems.append("the system does not see the copy's partitions")
        elif file_digest(loops, target + "p1") != kept:
            problems.append("a file on the copy differs")
        if not result.get("verified") or result.get("skipped"):
            problems.append("unexpected result %r" % result)
        check("a drive is copied block for block onto a larger one, and "
              "the copy's partition table is completed for its size",
              not problems, "; ".join(problems))
    except diskctl.HelperError as exc:
        failed("a drive is copied block for block", str(exc))

    small = loops.create(200 * MIB, fill=b"\xaa")
    try:
        do(clone_request(source, small))
        failed("a smaller target is refused", "it was accepted")
    except diskctl.HelperError as exc:
        check("a smaller target is refused before anything is written",
              "smaller" in str(exc)
              and pattern_blocks(loops.devices[small], b"\xaa")
              == 200 * MIB // 4096, str(exc))
    for request, name, word in (
            (clone_request(source, source), "a drive onto itself",
             "cannot go onto"),
            (clone_request(source, target + "p1"), "a drive onto a "
             "partition", "whole drive")):
        try:
            do(request)
            failed("%s is refused" % name, "it was accepted")
        except diskctl.HelperError as exc:
            check("%s is refused" % name, word in str(exc), str(exc))
    loops.detach(small)
    loops.detach(target)

    # ---- a drive with an unreadable patch ----------------------------------
    heading("A source with unreadable blocks")
    if shutil.which("dmsetup") is None:
        skipped("unreadable blocks", "dmsetup is not installed")
    else:
        bad_sector = 100 * MIB // 512
        bad = loops.with_bad_block(source, bad_sector)
        target = loops.create(300 * MIB, fill=b"\xaa")
        try:
            do(clone_request(bad, target))
            failed("an unreadable block stops an ordinary copy",
                   "the copy went through")
        except diskctl.HelperError as exc:
            check("an unreadable block stops the copy and says how to go "
                  "on", "Skip unreadable areas" in str(exc), str(exc))
        try:
            result = do(clone_request(bad, target, skip_errors=True))
            problems = []
            want = [[bad_sector * 512, 4096]]
            if result.get("skipped") != want:
                problems.append("skipped %r, expected %r"
                                % (result.get("skipped"), want))
            with open(source, "rb") as a, open(target, "rb") as b:
                same = True
                offset = 0
                while offset < 300 * MIB:
                    one, two = a.read(MIB), b.read(MIB)
                    if one != two:
                        hole = bad_sector * 512 - offset
                        if not (0 <= hole < MIB
                                and one[:hole] == two[:hole]
                                and two[hole:hole + 4096] == b"\0" * 4096
                                and one[hole + 4096:] == two[hole + 4096:]):
                            same = False
                    offset += MIB
            if not same:
                problems.append("the copy differs outside the unreadable "
                                "block, or the block is not zeros")
            check("with 'skip unreadable areas' everything else is copied "
                  "and the skipped range is reported exactly", not problems,
                  "; ".join(problems))
        except diskctl.HelperError as exc:
            failed("skipping unreadable areas", str(exc))
        loops.unmap(bad)
        loops.detach(target)
    loops.detach(source)

    # ---- a whole drive, used blocks only -----------------------------------
    heading("Cloning a drive, used blocks only")
    source = loops.create(900 * MIB, fill=b"\xaa")
    boot_code = os.urandom(440)
    sh("sfdisk", "--quiet", source,
       stdin="label: gpt\nsize=150MiB, type=L\nsize=150MiB, type=EBD0A0A2-"
             "B9E5-4433-87C0-68B6B72699C7\nsize=320MiB, type=L\n"
             "size=60MiB, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n")
    with open(loops.own(source), "r+b") as handle:
        handle.write(boot_code)
    sh("udevadm", "settle")
    made_with = {1: ("ext4", ["mkfs.ext4", "-q", "-F", "-L", "Linux"]),
                 2: ("ntfs", ["mkfs.ntfs", "--quick", "--force", "--label",
                              "Windows"]),
                 3: ("xfs", ["mkfs.xfs", "-q", "-f", "-L", "Xfs"]),
                 4: ("vfat", ["mkfs.fat", "-n", "FAT"])}
    kept = {}
    usable = all(shutil.which(argv[0]) for _fs, argv in made_with.values())
    if not usable:
        skipped("used-blocks copy of a drive", "a mkfs tool is missing")
    else:
        for number, (fstype, argv) in made_with.items():
            sh(*argv, loops.own("%sp%d" % (source, number)))
            kept[number] = populate(loops, "%sp%d" % (source, number))
        sh("sync")
        target = loops.create(1000 * MIB, fill=b"\x55")
        try:
            result = do(clone_request(source, target, mode="smart"))
            sh("udevadm", "settle")
            problems = []
            methods = [m["method"] for m in result.get("methods", [])]
            if methods != ["e2image", "ntfsclone", "xfs_copy", "exact"]:
                problems.append("methods were %r" % methods)
            if partitions_of(source) != partitions_of(target):
                problems.append("the partition tables differ")
            if table(source).get("id") != table(target).get("id"):
                problems.append("the disk identifier differs")
            if gpt_sound(target) is False:
                problems.append("the copy's GPT is not sound")
            with open(target, "rb") as handle:
                if handle.read(440) != boot_code:
                    problems.append("the boot code was not carried over")
            for number, (fstype, _argv) in made_with.items():
                one, two = ("%sp%d" % (d, number) for d in (source, target))
                if file_digest(loops, two) != kept[number]:
                    problems.append("%s: a file differs" % fstype)
                a, b = probe(one), probe(two)
                if (a.get("UUID"), a.get("LABEL")) != (b.get("UUID"),
                                                       b.get("LABEL")):
                    problems.append("%s: identity differs" % fstype)
                if fsck_clean(fstype, two) is False:
                    problems.append("%s: fsck reports errors" % fstype)
            # Proof that unused blocks were left alone: the target's own
            # filler is still there inside the first partition.
            start = partitions_of(target)[0][0] * 512
            untouched = 0
            with open(target, "rb") as handle:
                handle.seek(start)
                for _ in range(150 * MIB // 4096):
                    if handle.read(4096) == b"\x55" * 4096:
                        untouched += 1
            if untouched < 1000:
                problems.append("only %d blocks of the target were left "
                                "unwritten" % untouched)
            check("each filesystem is copied by its own tool, the rest "
                  "block for block (%d unused blocks never written)"
                  % untouched, not problems, "; ".join(problems))
        except diskctl.HelperError as exc:
            failed("used-blocks copy of a drive", str(exc))
        loops.detach(target)

        # Onto a drive smaller than the source but large enough for its
        # partitions.
        target = loops.create(760 * MIB, fill=b"\x55")
        try:
            do(clone_request(source, target))
            failed("an exact copy onto a smaller drive is refused",
                   "it was accepted")
        except diskctl.HelperError as exc:
            check("an exact copy onto a smaller drive is refused",
                  "smaller" in str(exc), str(exc))
        try:
            do(clone_request(source, target, mode="smart"))
            sh("udevadm", "settle")
            good = (partitions_of(source) == partitions_of(target)
                    and gpt_sound(target) is not False
                    and file_digest(loops, target + "p3") == kept[3])
            check("a used-blocks copy fits a smaller drive that still holds "
                  "every partition", good)
        except diskctl.HelperError as exc:
            failed("used-blocks copy onto a smaller drive", str(exc))
        loops.detach(target)

        # A damaged filesystem is not copied by its structure.
        point = loops.mount(source + "p1")
        with open(os.path.join(point, "victim"), "w") as handle:
            handle.write("x" * 50000)
        loops.unmount(point)
        sh("debugfs", "-w", "-R", "clri /victim", loops.own(source + "p1"),
           check_code=False)
        target = loops.create(1000 * MIB, fill=b"\x55")
        try:
            do(clone_request(source, target, mode="smart"))
            failed("a damaged filesystem is not copied by its contents",
                   "it was accepted")
        except diskctl.HelperError as exc:
            check("a damaged filesystem is refused for a used-blocks copy, "
                  "the target untouched, with what to do instead",
                  "exact copy" in str(exc) and "Repair it first" in str(exc)
                  and pattern_blocks(loops.devices[target], b"\x55")
                  == 1000 * MIB // 4096, str(exc))
        loops.detach(target)
    loops.detach(source)

    # ---- one partition -----------------------------------------------------
    heading("Cloning a partition")
    source = loops.create(400 * MIB)
    sh("sfdisk", "--quiet", source,
       stdin="label: gpt\nsize=150MiB, type=L\nsize=200MiB, type=L\n")
    sh("udevadm", "settle")
    sh("mkfs.ext4", "-q", "-F", "-L", "Keep", loops.own(source + "p1"))
    kept = populate(loops, source + "p1")
    point = loops.mount(source + "p1")          # mounted when asked
    for mode in ("exact", "smart"):
        sh("wipefs", "--all", "--quiet", loops.own(source + "p2"))
        try:
            result = do(clone_request(source + "p1", source + "p2",
                                      mode=mode))
            sh("udevadm", "settle")
            a, b = probe(source + "p1"), probe(source + "p2")
            good = (file_digest(loops, source + "p2") == kept
                    and a.get("UUID") == b.get("UUID")
                    and fsck_clean("ext4", source + "p2")
                    and result["methods"][0]["method"] == (
                        "exact" if mode == "exact" else "e2image"))
            check("a partition is copied onto a larger one (%s), after the "
                  "source was unmounted" % mode, good, repr(result))
        except diskctl.HelperError as exc:
            failed("a partition is copied (%s)" % mode, str(exc))
    if point in loops.mounts:
        loops.mounts.remove(point)
        subprocess.run(["umount", point], stderr=subprocess.DEVNULL)
        os.rmdir(point)
    loops.detach(source)

    # ---- stopping ----------------------------------------------------------
    heading("Stopping a copy")
    source = loops.create(4096 * MIB)
    target = loops.create(4096 * MIB)

    def stop_on_progress(event):
        if event.get("event") == "progress":
            client.cancel()

    try:
        do(clone_request(source, target), on_event=stop_on_progress)
        failed("a copy can be stopped", "it ran to the end")
    except diskctl.Cancelled as exc:
        check("a copy can be stopped, and says the target is incomplete",
              "incomplete copy" in str(exc), str(exc))
    except diskctl.HelperError as exc:
        failed("a copy can be stopped", str(exc))
    loops.detach(target)
    loops.detach(source)

    # ---- image files -------------------------------------------------------
    heading("Image files")
    source = loops.create(200 * MIB)
    with open(loops.own(source), "r+b") as handle:
        handle.write(os.urandom(64 * MIB))       # the rest stays zeros
    sh("sfdisk", "--quiet", source, stdin="label: gpt\nsize=150MiB, type=L\n")
    sh("udevadm", "settle")
    sh("mkfs.ext4", "-q", "-F", loops.own(source + "p1"))
    kept = populate(loops, source + "p1")
    sh("sync")
    original = sha(source)

    plain = os.path.join(work, "drive.img")
    packed = os.path.join(work, "drive.img.gz")
    for path, compress in ((plain, False), (packed, True)):
        name = "a drive is saved as %s image" % (
            "a compressed" if compress else "an")
        try:
            result = do(dict(ask("image_create", source), path=path,
                             compress=compress))
            problems = []
            if compress:
                out = subprocess.run(["gzip", "-dc", path],
                                     stdout=subprocess.PIPE).stdout
                found = hashlib.sha256(out).hexdigest()
                if os.path.getsize(path) >= 200 * MIB:
                    problems.append("it is not smaller than the drive")
            else:
                found = sha(path)
            if found != original or result.get("sha256") != original:
                problems.append("the image differs from the drive")
            owner = os.stat(path).st_uid
            if caller is not None and owner != caller:
                problems.append("the file belongs to uid %d, not %d"
                                % (owner, caller))
            if not result.get("verified"):
                problems.append("it was not verified")
            check("%s, verified, and belongs to the user" % name,
                  not problems, "; ".join(problems))
        except diskctl.HelperError as exc:
            failed(name, str(exc))

    try:
        do(dict(ask("image_create", source), path=plain))
        failed("an existing file is not overwritten unasked",
               "it was overwritten")
    except diskctl.HelperError as exc:
        check("an existing file is not overwritten unasked",
              "already exists" in str(exc) and sha(plain) == original,
              str(exc))

    point = loops.mount(source + "p1")
    inside = os.path.join(point, "self.img")
    try:
        do(dict(ask("image_create", source), path=inside))
        failed("an image is not saved on the drive it is of",
               "it was accepted")
    except diskctl.HelperError as exc:
        check("an image is not saved on the drive it is of",
              "itself" in str(exc) and not os.path.exists(inside), str(exc))
    loops.unmount(point)

    for path, kind in ((plain, "an"), (packed, "a compressed")):
        target = loops.create(260 * MIB, fill=b"\xaa")
        name = "%s image is written back onto a larger drive" % kind
        try:
            result = do(dict(ask("image_restore", target), path=path))
            sh("udevadm", "settle")
            problems = []
            # Apart from the partition table's two copies, which are redone
            # for the larger drive. Against the image, not the drive it was
            # made from: that has been mounted since and so has changed.
            with open(plain, "rb") as a, open(target, "rb") as b:
                for handle in (a, b):
                    handle.seek(MIB)
                if a.read(198 * MIB) != b.read(198 * MIB):
                    problems.append("the drive differs from the image")
            if gpt_sound(target) is False:
                problems.append("its GPT is not sound")
            if not os.path.exists(target + "p1") \
                    or file_digest(loops, target + "p1") != kept:
                problems.append("the file on it differs")
            if result.get("table") != "gpt":
                problems.append("result says table %r" % result.get("table"))
            check(name, not problems, "; ".join(problems))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
        loops.detach(target)

    for path, kind in ((plain, "an"), (packed, "a compressed")):
        target = loops.create(100 * MIB, fill=b"\xaa")
        try:
            do(dict(ask("image_restore", target), path=path))
            failed("%s image too large for the drive is refused" % kind,
                   "it was accepted")
        except diskctl.HelperError as exc:
            check("%s image too large for the drive is refused before "
                  "anything is written" % kind,
                  "does not fit" in str(exc)
                  and pattern_blocks(loops.devices[target], b"\xaa")
                  == 100 * MIB // 4096, str(exc))
        loops.detach(target)

    broken = os.path.join(work, "broken.img.gz")
    with open(packed, "rb") as handle:
        data = handle.read()
    with open(broken, "wb") as handle:
        handle.write(data[:len(data) // 2])
    target = loops.create(260 * MIB, fill=b"\xaa")
    try:
        do(dict(ask("image_restore", target), path=broken))
        failed("a damaged image is refused", "it was accepted")
    except diskctl.HelperError as exc:
        check("a damaged compressed image is refused before anything is "
              "written", "damaged" in str(exc)
              and pattern_blocks(loops.devices[target], b"\xaa")
              == 260 * MIB // 4096, str(exc))
    loops.detach(target)

    # An installer image, as written to a memory stick.
    if shutil.which("xorriso") and shutil.which("mkfs.fat"):
        tree = os.path.join(work, "tree")
        os.mkdir(tree)
        with open(os.path.join(tree, "payload.bin"), "wb") as handle:
            handle.write(os.urandom(8 * MIB))
        boot = os.path.join(work, "efi.img")
        with open(boot, "wb") as handle:
            handle.truncate(4 * MIB)
        sh("mkfs.fat", boot)
        iso = os.path.join(work, "live.iso")
        sh("xorriso", "-as", "mkisofs", "-o", iso, "-V", "Live-Image",
           "-partition_offset", "16", "-append_partition", "2", "0xef", boot,
           tree)
        target = loops.create(300 * MIB, fill=b"\xaa")
        try:
            result = do(dict(ask("image_restore", target), path=iso))
            sh("udevadm", "settle")
            size = os.path.getsize(iso)
            check("an installer image is written to a drive and reads back "
                  "the same", sha(target, size) == sha(iso)
                  and probe(target).get("TYPE") == "iso9660"
                  and result.get("verified"), repr(result))
        except diskctl.HelperError as exc:
            failed("an installer image is written to a drive", str(exc))
        loops.detach(target)
    else:
        skipped("an installer image", "xorriso is not installed")

    big = os.path.join(work, "big.img")
    with open(big, "wb") as handle:
        handle.truncate(4096 * MIB)
    target = loops.create(4096 * MIB)
    try:
        do(dict(ask("image_restore", target), path=big),
           on_event=stop_on_progress)
        failed("writing an image can be stopped", "it ran to the end")
    except diskctl.Cancelled as exc:
        check("writing an image can be stopped, and says the drive is not "
              "usable", "part of the image" in str(exc), str(exc))
    except diskctl.HelperError as exc:
        failed("writing an image can be stopped", str(exc))
    loops.detach(target)
    os.remove(big)

    target = os.path.join(work, "stopped.img")
    big_source = loops.create(4096 * MIB)
    try:
        do(dict(ask("image_create", big_source), path=target),
           on_event=stop_on_progress)
        failed("saving an image can be stopped", "it ran to the end")
    except diskctl.Cancelled as exc:
        check("a stopped image leaves no file behind",
              not os.path.exists(target), str(exc))
    except diskctl.HelperError as exc:
        failed("saving an image can be stopped", str(exc))
    loops.detach(big_source)
    loops.detach(source)
    shutil.rmtree(work, ignore_errors=True)


def sysfs_bytes(device, attribute):
    with open("/sys/class/block/%s/%s" % (os.path.basename(device),
                                          attribute)) as handle:
        return int(handle.read()) * 512


def fill_files(loops, device, megabytes, options=None):
    """Write that much random data as files; returns {name: digest}."""
    point = loops.mount(device, options)
    found = {}
    for index in range(megabytes):
        path = os.path.join(point, "file%03d.bin" % index)
        with open(path, "wb") as handle:
            handle.write(os.urandom(MIB))
        found["file%03d.bin" % index] = digest(path)
    sh("sync")
    loops.unmount(point)
    return found


def files_intact(loops, device, wanted):
    try:
        point = loops.mount(device, "ro")
    except RuntimeError as exc:
        return "does not mount: %s" % str(exc).splitlines()[-1]
    try:
        for name, want in wanted.items():
            try:
                if digest(os.path.join(point, name)) != want:
                    return "%s differs" % name
            except OSError as exc:
                return "%s: %s" % (name, exc)
    finally:
        loops.unmount(point)
    return ""


MKFS_QUIET = {"ext4": ["mkfs.ext4", "-q", "-F"],
              "ntfs": ["mkfs.ntfs", "--quick", "--force"],
              "btrfs": ["mkfs.btrfs", "-q", "-f"],
              "xfs": ["mkfs.xfs", "-q", "-f"],
              "f2fs": ["mkfs.f2fs", "-q", "-f"],
              "vfat": ["mkfs.fat"], "exfat": ["mkfs.exfat"],
              "swap": ["mkswap", "-q"]}


def check_partitions(loops, client, caller):
    do = runner(loops, client)
    import random
    rng = random.Random(20261110)

    def cleanup_state():
        for folder in ("/var/lib/ksyssupervisor/tables",
                       "/var/lib/ksyssupervisor/moves"):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if name.startswith("loop"):
                    try:
                        os.unlink(os.path.join(folder, name))
                    except OSError:
                        pass

    # ---- a new table, creating, deleting ------------------------------------
    heading("A new partition table, and partitions in it")
    disk = loops.create(700 * MIB)
    sh("sfdisk", "--quiet", disk, stdin="label: dos\nsize=100MiB, type=83\n")
    sh("udevadm", "settle")
    sh("mkfs.ext4", "-q", "-F", loops.own(disk + "p1"))
    try:
        result = do(ask("table_new", disk, table="gpt"))
        saved = result.get("saved")
        check("a new GPT replaces the old table, which is saved first",
              probe(disk).get("PTTYPE") == "gpt"
              and not table(disk).get("partitions")
              and not os.path.exists(disk + "p1")
              and saved and "label: dos" in open(saved).read(), repr(result))
    except diskctl.HelperError as exc:
        failed("a new partition table", str(exc))

    made = {}
    for name, fstype, start, size, label in (
            ("first", "ext4", MIB, 200 * MIB, "First"),
            ("second", "exfat", 201 * MIB, 100 * MIB, "SECOND"),
            ("bare", "", 301 * MIB, 50 * MIB, "")):
        try:
            result = do(ask("part_create", disk, start=start, size=size,
                            fstype=fstype, label=label, owner=True))
            sh("udevadm", "settle")
            part = result["device"]
            made[name] = part
            found = probe(part)
            good = (sysfs_bytes(part, "start") == start
                    and sysfs_bytes(part, "size") == size
                    and found.get("TYPE", "") == fstype
                    and found.get("LABEL", "") == label)
            check("a %s partition is created where asked (%s)"
                  % (fstype or "bare", os.path.basename(part)), good,
                  "%r %r" % (result, found))
        except diskctl.HelperError as exc:
            failed("a %s partition is created" % (fstype or "bare"),
                   str(exc))
    before = partitions_of(disk)
    for start, size, what in ((100 * MIB, 50 * MIB, "inside another"),
                              (351 * MIB, 900 * MIB, "past the end")):
        try:
            do(ask("part_create", disk, start=start, size=size,
                   fstype="ext4", label=""))
            failed("a partition %s is refused" % what, "it was created")
        except diskctl.HelperError as exc:
            check("a partition %s is refused and the table left alone"
                  % what, "not unallocated space" in str(exc)
                  and partitions_of(disk) == before, str(exc))

    if "first" in made:
        kept = fill_files(loops, made["first"], 5)
        point = loops.mount(made["first"])
        try:
            result = do(ask("part_create", disk, start=351 * MIB,
                            size=100 * MIB, fstype="ext4", label="Later"))
            sh("udevadm", "settle")
            still = digest(os.path.join(point, "file000.bin")) \
                == kept["file000.bin"]
            check("a partition is created while another on the drive is in "
                  "use", os.path.exists(result["device"]) and still
                  and probe(result["device"]).get("LABEL") == "Later")
            made["later"] = result["device"]
        except diskctl.HelperError as exc:
            failed("creating with a neighbour in use", str(exc))

        # Deleting.
        try:
            do(ask("part_delete", made["second"]))
            sh("udevadm", "settle")
            check("a partition is deleted while another on the drive is in "
                  "use", not os.path.exists(made["second"])
                  and len(partitions_of(disk)) == len(before)
                  and digest(os.path.join(point, "file001.bin"))
                  == kept["file001.bin"])
        except diskctl.HelperError as exc:
            failed("deleting a partition", str(exc))
        try:
            do(ask("part_delete", made["first"]))
            sh("udevadm", "settle")
            mounts = sh("findmnt", "--noheadings", "--source", made["first"],
                        check_code=False).stdout.strip()
            check("a mounted partition is unmounted, then deleted",
                  not os.path.exists(made["first"]) and not mounts)
            loops.mounts.remove(point)
            os.rmdir(point)
        except diskctl.HelperError as exc:
            failed("deleting a mounted partition", str(exc))
            loops.unmount(point)
    loops.detach(disk)

    # ---- MBR: four primaries, and logical partitions ------------------------
    heading("MBR limits")
    disk = loops.create(400 * MIB)
    sh("sfdisk", "--quiet", disk,
       stdin="label: dos\nsize=20MiB, type=83\nsize=20MiB, type=83\n"
             "size=20MiB, type=83\nsize=20MiB, type=83\n")
    sh("udevadm", "settle")
    try:
        do(ask("part_create", disk, start=100 * MIB, size=50 * MIB,
               fstype="ext4", label=""))
        failed("a fifth primary partition is refused", "it was created")
    except diskctl.HelperError as exc:
        check("a fifth primary partition is refused, with the way out",
              "four partitions" in str(exc) and "GPT" in str(exc), str(exc))
    sh("sfdisk", "--quiet", disk,
       stdin="label: dos\nsize=50MiB, type=83\nstart=60MiB, size=300MiB, "
             "type=5\n")
    sh("udevadm", "settle")
    try:
        result = do(ask("part_create", disk, start=62 * MIB, size=80 * MIB,
                        fstype="ext4", label="Logical"))
        sh("udevadm", "settle")
        number = int(re.search(r"(\d+)$", result["device"]).group(1))
        check("room inside an extended partition gives a logical partition",
              number >= 5 and probe(result["device"]).get("LABEL")
              == "Logical", repr(result))
        try:
            do(ask("part_delete", disk + "p2"))
            failed("an extended partition holding others is not deleted",
                   "it was deleted")
        except diskctl.HelperError as exc:
            check("an extended partition holding others is not deleted",
                  "logical partitions" in str(exc), str(exc))
    except diskctl.HelperError as exc:
        failed("a logical partition", str(exc))
    loops.detach(disk)

    # ---- resizing -----------------------------------------------------------
    heading("Resizing")
    layout = ("label: gpt\nsize=%dMiB, type=L\nstart=%dMiB, size=100MiB, "
              "type=L\n")

    def prepared(fstype, size, room):
        """A disk with the partition under test, free room after it, and a
        neighbour holding a file - mounted throughout."""
        disk = loops.create((size + room + 120) * MIB)
        sh("sfdisk", "--quiet", disk,
           stdin=layout % (size, 1 + size + room))
        sh("udevadm", "settle")
        sh(*MKFS_QUIET[fstype], loops.own(disk + "p1"))
        sh("mkfs.ext4", "-q", "-F", loops.own(disk + "p2"))
        sh("udevadm", "settle")
        return disk

    def resized(disk, megabytes):
        result = do(ask("part_resize", disk + "p1", size=megabytes * MIB))
        sh("udevadm", "settle")
        return result

    for fstype, size, room, data in (("ext4", 300, 300, 40),
                                     ("ntfs", 300, 300, 40),
                                     ("btrfs", 400, 300, 40)):
        if shutil.which(MKFS_QUIET[fstype][0]) is None:
            skipped("%s resizing" % fstype, "mkfs is missing")
            continue
        disk = prepared(fstype, size, room)
        part = disk + "p1"
        kept = fill_files(loops, part, data)
        neighbour = fill_files(loops, disk + "p2", 3)
        guard = loops.mount(disk + "p2")
        problems = []
        sizes = []
        try:
            limits = do(ask("part_limits", part))
            smallest = limits["smallest"] // MIB
            largest = limits["largest"] // MIB
            if not (data < smallest <= size and largest == size + room):
                problems.append("limits %r" % limits)
            # Down, up, and to sizes picked at random in between.
            sizes = [smallest, largest] + [
                rng.randint(smallest, largest) for _ in range(4)]
            current = size
            for target in sizes:
                if target == current:
                    continue
                result = resized(disk, target)
                current = target
                if sysfs_bytes(part, "size") != target * MIB:
                    problems.append("to %d MiB: the partition is %d bytes"
                                    % (target, sysfs_bytes(part, "size")))
                if fsck_clean(fstype, part) is False:
                    problems.append("to %d MiB: fsck reports errors" % target)
                wrong = files_intact(loops, part, kept)
                if wrong:
                    problems.append("to %d MiB: %s" % (target, wrong))
                point = loops.mount(part, "ro")
                total = os.statvfs(point)
                total = total.f_blocks * total.f_frsize
                loops.unmount(point)
                if not (target * MIB * 0.80 <= total <= target * MIB):
                    problems.append("to %d MiB: the filesystem is %d MiB"
                                    % (target, total // MIB))
                if problems:
                    break
            if digest(os.path.join(guard, "file000.bin")) \
                    != neighbour["file000.bin"]:
                problems.append("the neighbouring partition changed")
        except diskctl.HelperError as exc:
            problems.append(str(exc))
        check("%s is shrunk and grown to %s MiB with every file intact and "
              "the neighbour untouched" % (fstype, sizes), not problems,
              "; ".join(problems))

        try:
            resized(disk, max(4, data // 2))
            failed("%s is not shrunk below its contents" % fstype,
                   "it was accepted")
        except diskctl.HelperError as exc:
            check("%s is not shrunk below what its contents need" % fstype,
                  "cannot be made smaller than" in str(exc)
                  and not files_intact(loops, part, kept), str(exc))
        try:
            resized(disk, size + room + 50)
            failed("%s is not grown into the next partition" % fstype,
                   "it was accepted")
        except diskctl.HelperError as exc:
            check("%s is not grown into the next partition" % fstype,
                  "only room to grow" in str(exc), str(exc))
        loops.unmount(guard)
        loops.detach(disk)

    # ext4 and Btrfs grown while mounted and in use.
    for fstype in ("ext4", "btrfs", "xfs"):
        if shutil.which(MKFS_QUIET[fstype][0]) is None:
            continue
        disk = prepared(fstype, 400, 200)
        part = disk + "p1"
        kept = fill_files(loops, part, 20)
        point = loops.mount(part)
        try:
            result = resized(disk, 600)
            total = os.statvfs(point)
            total = total.f_blocks * total.f_frsize
            mounts = sh("findmnt", "--noheadings", "--source", part,
                        check_code=False).stdout.strip()
            check("%s is grown while mounted and in use" % fstype,
                  sysfs_bytes(part, "size") == 600 * MIB and mounts
                  and total > 500 * MIB
                  and digest(os.path.join(point, "file005.bin"))
                  == kept["file005.bin"], "%r, filesystem %d MiB"
                  % (result, total // MIB))
        except diskctl.HelperError as exc:
            failed("%s is grown while mounted" % fstype, str(exc))
        loops.unmount(point)
        if fstype == "xfs":
            try:
                resized(disk, 450)
                failed("XFS is not shrunk", "it was accepted")
            except diskctl.HelperError as exc:
                check("XFS is refused for shrinking, with the reason",
                      "never smaller" in str(exc)
                      and sysfs_bytes(part, "size") == 600 * MIB, str(exc))
        loops.detach(disk)

    for fstype in ("vfat", "exfat"):
        if shutil.which(MKFS_QUIET[fstype][0]) is None:
            continue
        disk = prepared(fstype, 200, 100)
        try:
            resized(disk, 250)
            failed("%s is not resized" % fstype, "it was accepted")
        except diskctl.HelperError as exc:
            check("%s is refused for resizing, with what to do instead"
                  % fstype, "cannot be resized" in str(exc)
                  and "Copy the files off" in str(exc)
                  and sysfs_bytes(disk + "p1", "size") == 200 * MIB,
                  str(exc))
        loops.detach(disk)

    disk = prepared("swap", 100, 100)
    identity = probe(disk + "p1").get("UUID")
    try:
        resized(disk, 180)
        check("swap is resized and keeps its ID",
              probe(disk + "p1").get("UUID") == identity
              and probe(disk + "p1").get("TYPE") == "swap"
              and sysfs_bytes(disk + "p1", "size") == 180 * MIB)
    except diskctl.HelperError as exc:
        failed("swap is resized", str(exc))
    loops.detach(disk)

    if shutil.which("mkfs.f2fs"):
        disk = prepared("f2fs", 200, 200)
        kept = fill_files(loops, disk + "p1", 10)
        try:
            resized(disk, 380)
            check("F2FS is grown with its files intact",
                  not files_intact(loops, disk + "p1", kept)
                  and sysfs_bytes(disk + "p1", "size") == 380 * MIB)
        except diskctl.HelperError as exc:
            failed("F2FS is grown", str(exc))
        loops.detach(disk)

    # A damaged filesystem is not resized.
    disk = prepared("ext4", 300, 200)
    point = loops.mount(disk + "p1")
    with open(os.path.join(point, "victim"), "w") as handle:
        handle.write("x" * 50000)
    loops.unmount(point)
    sh("debugfs", "-w", "-R", "clri /victim", loops.own(disk + "p1"),
       check_code=False)
    before = partitions_of(disk)
    try:
        resized(disk, 400)
        failed("a damaged filesystem is not resized", "it was accepted")
    except diskctl.HelperError as exc:
        check("a damaged filesystem is not resized, and nothing is changed",
              "Repair it first" in str(exc)
              and partitions_of(disk) == before, str(exc))
    loops.detach(disk)

    # ---- moving -------------------------------------------------------------
    heading("Moving")

    def movable(fstype, megabytes=200, files=30):
        """|1 MiB| guard (60) | free (200) | partition | free (200) |
        guard (60) |"""
        disk = loops.create((60 + 200 + megabytes + 200 + 60 + 2) * MIB)
        sh("sfdisk", "--quiet", disk, stdin=(
            "label: gpt\nstart=1MiB, size=60MiB, type=L\n"
            "start=261MiB, size=%dMiB, type=L\n"
            "start=%dMiB, size=60MiB, type=L\n"
            % (megabytes, 261 + megabytes + 200)))
        sh("udevadm", "settle")
        for number in (1, 3):
            with open(loops.own("%sp%d" % (disk, number)), "r+b") as handle:
                handle.write(os.urandom(60 * MIB))
        sh(*MKFS_QUIET[fstype], loops.own(disk + "p2"))
        sh("udevadm", "settle")
        kept = fill_files(loops, disk + "p2", files)
        return disk, kept

    def moved(disk, megabyte):
        result = do(ask("part_move", disk + "p2", start=megabyte * MIB))
        sh("udevadm", "settle")
        return result

    for fstype in ("ext4", "ntfs"):
        if shutil.which(MKFS_QUIET[fstype][0]) is None:
            skipped("%s moving" % fstype, "mkfs is missing")
            continue
        disk, kept = movable(fstype)
        guards = (sha(disk + "p1"), sha(disk + "p3"))
        content = sha(disk + "p2")
        problems = []
        # A little to the right, a little to the left (both overlapping
        # where it was), all the way to each end, then at random.
        places = [264, 262, 461, 61, 300] + [rng.randint(61, 461)
                                             for _ in range(4)]
        try:
            current = 261
            for place in places:
                if place == current:
                    continue
                moved(disk, place)
                current = place
                if sysfs_bytes(disk + "p2", "start") != place * MIB:
                    problems.append("to %d MiB: starts at %d" % (
                        place, sysfs_bytes(disk + "p2", "start")))
                # NTFS is told where it now starts, which changes four
                # bytes of it on purpose: its files are compared instead.
                if fstype != "ntfs" and sha(disk + "p2") != content:
                    problems.append("to %d MiB: the contents differ" % place)
                if fsck_clean(fstype, disk + "p2") is False:
                    problems.append("to %d MiB: fsck reports errors" % place)
                if fstype == "ntfs":
                    wrong = files_intact(loops, disk + "p2", kept)
                    if wrong:
                        problems.append("to %d MiB: %s" % (place, wrong))
                    with open(disk + "p2", "rb") as handle:
                        handle.seek(0x1C)
                        hidden = int.from_bytes(handle.read(4), "little")
                    if hidden != place * MIB // 512:
                        problems.append("to %d MiB: the filesystem still "
                                        "says %d sectors precede it"
                                        % (place, hidden))
                    content = sha(disk + "p2")
                if problems:
                    break
            wrong = files_intact(loops, disk + "p2", kept)
            if wrong:
                problems.append(wrong)
            if (sha(disk + "p1"), sha(disk + "p3")) != guards:
                problems.append("a neighbouring partition changed")
            if gpt_sound(disk) is False:
                problems.append("the GPT is not sound")
        except diskctl.HelperError as exc:
            problems.append(str(exc))
        check("%s is moved to %s MiB, overlapping and not, byte for byte "
              "the same" % (fstype, places), not problems,
              "; ".join(problems))
        for place, what in ((30, "onto the partition before it"),
                            (500, "onto the partition after it")):
            try:
                moved(disk, place)
                failed("a move %s is refused" % what, "it was accepted")
            except diskctl.HelperError as exc:
                check("a move %s is refused" % what, "no room" in str(exc),
                      str(exc))
        loops.detach(disk)

    # ---- a move cut short ----------------------------------------------------
    heading("A move interrupted by a crash")
    for direction, place in (("right", 271), ("left", 251)):
        disk, kept = movable("ext4", megabytes=300, files=120)
        content = sha(disk + "p2")
        guards = (sha(disk + "p1"), sha(disk + "p3"))
        seen = []
        victim = diskctl.DiskHelperClient(
            path=HELPER, launcher=lambda path: [sys.executable, path])
        victim.start()

        def kill_midway(event):
            if event.get("event") == "progress" \
                    and event.get("done", 0) > 60 * MIB and not seen:
                seen.append(event["done"])
                victim._proc.kill()

        loops.own(disk + "p2")
        try:
            victim.run(ask("part_move", disk + "p2", start=place * MIB),
                       on_event=kill_midway)
            interrupted = False
        except diskctl.HelperError:
            interrupted = True
        try:
            victim.stop()
        except Exception:
            pass
        sh("udevadm", "settle")
        record = "/var/lib/ksyssupervisor/moves/%s-2.json" \
            % os.path.basename(disk)
        if not interrupted or not seen or not os.path.exists(record):
            failed("a move to the %s is cut short for the test" % direction,
                   "the move finished before it could be interrupted")
            loops.detach(disk)
            continue
        half = sha(disk + "p2") != content
        try:
            do(ask("part_move", disk + "p2", start=(place + 5) * MIB))
            failed("a half-moved partition is not moved again",
                   "it was accepted")
        except diskctl.HelperError as exc:
            check("a half-moved partition is not moved again (%s)"
                  % direction, "interrupted" in str(exc), str(exc))
        try:
            result = client.run({"op": "move_resume",
                                 "disk": os.path.basename(disk),
                                 "number": 2})
            sh("udevadm", "settle")
            problems = []
            if sysfs_bytes(disk + "p2", "start") != place * MIB:
                problems.append("it starts at %d" % sysfs_bytes(
                    disk + "p2", "start"))
            if sha(disk + "p2") != content:
                problems.append("the contents differ")
            if fsck_clean("ext4", disk + "p2") is False:
                problems.append("fsck reports errors")
            wrong = files_intact(loops, disk + "p2", kept)
            if wrong:
                problems.append(wrong)
            if (sha(disk + "p1"), sha(disk + "p3")) != guards:
                problems.append("a neighbouring partition changed")
            if os.path.exists(record):
                problems.append("the record of the move is still there")
            check("a move to the %s killed after %d MiB is taken up again "
                  "and ends byte for byte right (it was %s in between)"
                  % (direction, seen[0] // MIB,
                     "unusable" if half else "still whole"),
                  not problems, "; ".join(problems) or repr(result))
        except diskctl.HelperError as exc:
            failed("an interrupted move to the %s is finished" % direction,
                   str(exc))
        loops.detach(disk)
    cleanup_state()


def lab_files(folder):
    """Files of several kinds, some of them in folders; {path: digest}."""
    import struct
    import zipfile
    import zlib

    def png(path, side):
        raw = b"".join(b"\0" + os.urandom(side * 3) for _ in range(side))

        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data)))
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
                ">IIBBBBB", side, side, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    os.makedirs(os.path.join(folder, "Photos", "Trip 2024"))
    os.makedirs(os.path.join(folder, "Docs"))
    png(os.path.join(folder, "Photos", "Trip 2024", "beach.png"), 300)
    png(os.path.join(folder, "Photos", "cat.png"), 200)
    png(os.path.join(folder, "Photos", "kept.png"), 100)
    with open(os.path.join(folder, "Docs", "report.pdf"), "wb") as handle:
        handle.write(b"%PDF-1.4\n" + os.urandom(60000)
                     + b"\nstartxref\n9\n%%EOF\n")
    with zipfile.ZipFile(os.path.join(folder, "Docs", "budget.xlsx"),
                         "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>" * 3000)
    with open(os.path.join(folder, "video.bin"), "wb") as handle:
        handle.write(os.urandom(3 * MIB))
    found = {}
    for base, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(base, name)
            found[os.path.relpath(path, folder)] = digest(path)
    return found


DELETED_FILES = ("Photos/Trip 2024/beach.png", "Photos/cat.png",
                 "Docs/report.pdf", "Docs/budget.xlsx", "video.bin")


def check_recover(loops, client, caller):
    do = runner(loops, client)
    for tool in ("fls", "icat", "photorec"):
        if shutil.which(tool) is None:
            skipped("recovering files", "'%s' is not installed" % tool)
            return
    work = tempfile.mkdtemp(prefix="rec-", dir=loops.directory)
    os.chmod(loops.directory, 0o711)
    if caller is not None:
        os.chown(work, caller, -1)
    source = os.path.join(work, "source")
    os.mkdir(source)
    wanted = lab_files(source)

    def session_for(device):
        """A work folder as the application makes one, as the user, with
        a folder in it to recover into."""
        path = tempfile.mkdtemp(prefix="KSysSupervisor Search ", dir=work)
        diskrecover._mark(path)
        os.mkdir(os.path.join(path, "recovered"))
        if caller is not None:
            for name in (path, os.path.join(path, "scan.json"),
                         os.path.join(path, "recovered")):
                os.chown(name, caller, -1)
        return path

    def filled(fstype, megabytes=160, kind=None):
        device = made(loops, megabytes * MIB, fstype)
        if device is None:
            return None
        point = loops.mount(device, kind=kind)
        sh("cp", "-r", *[os.path.join(source, n)
                         for n in os.listdir(source)], point)
        sh("sync")
        loops.unmount(point)
        return device

    # ---- deleted files, with their names -------------------------------------
    heading("Deleted files")
    # Which driver deletes matters on NTFS: ntfs-3g leaves the record whole,
    # as Windows does; the kernel's ntfs3 (what a plain mount picks since
    # Fedora 45) takes the name out. The names are looked for after the
    # former; what the latter leaves is checked separately below.
    fuse = "ntfs-3g" if shutil.which("mount.ntfs-3g") else None
    for fstype in ("vfat", "exfat", "ntfs"):
        name = "deleted files on %s come back under their names" % fstype
        kind = fuse if fstype == "ntfs" else None
        if fstype == "ntfs" and kind is None:
            skipped(name, "ntfs-3g is not installed")
            continue
        device = filled(fstype, kind=kind)
        if device is None:
            skipped(name, "mkfs is missing")
            continue
        point = loops.mount(device, kind=kind)
        for path in DELETED_FILES:
            os.remove(os.path.join(point, path))
        os.rmdir(os.path.join(point, "Photos", "Trip 2024"))
        sh("sync")
        loops.unmount(point)
        session = session_for(device)
        problems = []
        try:
            result = do(dict(ask("recover_scan", device), session=session,
                             deleted=True, content=False))
            entries = diskrecover.scan_entries(result, session)
            listed = {e.path: e for e in entries}
            # FAT drops the first letter of a name that had no long form.
            missing = [p for p in DELETED_FILES if p not in listed]
            if missing:
                problems.append("not listed: %r (listed: %r)"
                                % (missing, sorted(listed)[:12]))
            if "Photos/kept.png" in listed:
                problems.append("a file that was never deleted is listed")
            for entry in entries:
                entry.chosen = entry.path in DELETED_FILES
            count = diskrecover.write_selection(session, entries)
            if caller is not None:
                os.chown(os.path.join(session, "selection.json"), caller, -1)
            outcome = do(dict(ask("recover_files", device), session=session,
                              target=os.path.join(session, "recovered")))
            if outcome.get("recovered") != count or outcome.get("failed"):
                problems.append("recovered %r of %d, failed %r" % (
                    outcome.get("recovered"), count, outcome.get("failed")))
            for path in DELETED_FILES:
                if path not in listed:
                    continue
                target = os.path.join(session, "recovered", path)
                if not os.path.exists(target):
                    problems.append("%s was not written" % path)
                elif digest(target) != wanted[path]:
                    problems.append("%s differs from the original" % path)
                elif caller is not None and os.stat(target).st_uid != caller:
                    problems.append("%s does not belong to the user" % path)
            diskrecover.attach_recovered(session, entries)
            counts = diskrecover.inspect_all(
                [e for e in entries if e.chosen])
            if counts.get(diskrecover.DAMAGED) \
                    or counts.get(diskrecover.UNREADABLE):
                problems.append("checked as %r" % counts)
            check("%s, byte for byte (%d listed, checked: %s)" % (
                name, len(entries), diskrecover.summary_text(counts)),
                not problems, "; ".join(problems))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
        loops.detach(device)

    # Deleted through the kernel's ntfs3: the names are gone, and the scan
    # must say nothing false about them - no stray or half-named entries.
    name = ("files deleted through the kernel's NTFS driver are not listed "
            "under wrong names")
    device = filled("ntfs", kind="ntfs3")
    if device is None:
        skipped(name, "mkfs.ntfs is missing")
    else:
        point = loops.mount(device, kind="ntfs3")
        for path in DELETED_FILES:
            os.remove(os.path.join(point, path))
        sh("sync")
        loops.unmount(point)
        session = session_for(device)
        try:
            result = do(dict(ask("recover_scan", device), session=session,
                             deleted=True, content=False))
            entries = diskrecover.scan_entries(result, session)
            names = {os.path.basename(p) for p in DELETED_FILES}
            stray = [e.path for e in entries
                     if os.path.basename(e.path) in names
                     and e.path not in DELETED_FILES]
            check("%s (%d listed)" % (name, len(entries)), not stray,
                  "listed elsewhere: %r" % stray)
        except diskctl.HelperError as exc:
            failed(name, str(exc))
        loops.detach(device)

    device = filled("ext4")
    point = loops.mount(device)
    os.remove(os.path.join(point, "Docs", "report.pdf"))
    loops.unmount(point)
    session = session_for(device)
    try:
        result = do(dict(ask("recover_scan", device), session=session,
                         deleted=True, content=False))
        check("ext4 is reported as keeping no record of deleted files",
              result["deleted"] == 0 and result["unlisted"]
              and result["unlisted"][0]["fstype"] == "ext4", repr(result))
    except diskctl.HelperError as exc:
        failed("ext4 deleted files", str(exc))

    # ---- refusals ---------------------------------------------------------------
    point = loops.mount(device)
    inside = os.path.join(point, "KSysSupervisor Recovery x")
    os.mkdir(inside)
    diskrecover._mark(inside)
    if caller is not None:
        os.chown(inside, caller, -1)
    try:
        do(dict(ask("recover_scan", device), session=inside, deleted=True))
        failed("recovering onto the drive being searched is refused",
               "it was accepted")
    except diskctl.HelperError as exc:
        check("recovering onto the drive being searched is refused",
              "itself" in str(exc), str(exc))
    loops.unmount(point)
    try:
        do(dict(ask("recover_scan", device), session=work, deleted=True))
        failed("a folder that is not a recovery folder is refused",
               "it was accepted")
    except diskctl.HelperError as exc:
        check("a folder that is not a recovery folder is refused",
              "not a recovery folder" in str(exc), str(exc))
    loops.detach(device)

    # ---- a whole drive, each partition ---------------------------------------------
    disk = loops.create(400 * MIB)
    sh("sfdisk", "--quiet", disk, stdin="label: gpt\nsize=180MiB, type="
       "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\nsize=180MiB, type=L\n")
    sh("udevadm", "settle")
    sh("mkfs.exfat", loops.own(disk + "p1"))
    sh("mkfs.ext4", "-q", "-F", loops.own(disk + "p2"))
    point = loops.mount(disk + "p1")
    shutil.copy(os.path.join(source, "Photos", "cat.png"), point)
    sh("sync")
    os.remove(os.path.join(point, "cat.png"))
    loops.unmount(point)
    session = session_for(disk)
    try:
        result = do(dict(ask("recover_scan", disk), session=session,
                         deleted=True))
        check("a whole drive is searched partition by partition",
              [v["device"] for v in result["volumes"]] == [disk + "p1"]
              and [u["device"] for u in result["unlisted"]] == [disk + "p2"]
              and result["deleted"] >= 1, repr(result))
    except diskctl.HelperError as exc:
        failed("searching a whole drive", str(exc))
    loops.detach(disk)

    # ---- after a format: by content ----------------------------------------------
    heading("Files found by content")
    device = filled("vfat")
    # Formatted over, the way a camera or Windows does it: a new filesystem
    # and nothing else. (mkfs.exfat by itself also discards every block of
    # a device that takes TRIM, after which there is nothing left to find -
    # which is what happens on an SSD, and what the application says.)
    sh("mkfs.exfat", "--no-discard", loops.own(device))
    session = session_for(device)
    try:
        result = do(dict(ask("recover_scan", device), session=session,
                         deleted=True, content=True))
        entries = diskrecover.scan_entries(result, session)
        found = [e for e in entries if e.source == diskrecover.FOUND]
        digests = {digest(e.file) for e in found}
        problems = []
        for path in ("Photos/Trip 2024/beach.png", "Photos/cat.png",
                     "Photos/kept.png", "Docs/report.pdf"):
            if wanted[path] not in digests:
                problems.append("%s was not found whole" % path)
        if caller is not None and any(os.stat(e.file).st_uid != caller
                                      for e in found):
            problems.append("found files do not belong to the user")
        kinds = sorted({e.category for e in found})
        counts = diskrecover.inspect_all(found)
        if not counts.get(diskrecover.INTACT):
            problems.append("nothing checked as intact: %r" % counts)
        for entry in found:
            entry.chosen = entry.category == "Pictures"
        kept = diskrecover.move_found(entries, os.path.join(session,
                                                            "recovered"))
        pictures = os.path.join(session, "recovered", "Found by content",
                                "Pictures")
        if kept < 3 or not os.path.isdir(pictures) \
                or len(os.listdir(pictures)) != kept:
            problems.append("kept %d pictures" % kept)
        if problems:
            log = os.path.join(session, "found", "photorec.log")
            try:
                with open(log, errors="replace") as handle:
                    problems.append("PhotoRec said: %s"
                                    % " / ".join(handle.read().split("\n")
                                                 [-14:]))
            except OSError as exc:
                problems.append("no PhotoRec log: %s" % exc)
            problems.append("result %r" % result)
        check("after a format, files are found by content, whole (%d "
              "found: %s; checked: %s)" % (
                  len(found), ", ".join(kinds),
                  diskrecover.summary_text(counts)), not problems,
              "; ".join(problems))
    except diskctl.HelperError as exc:
        failed("files found by content", str(exc))
    loops.detach(device)

    # A Deep Scan limited to some kinds: PhotoRec is told so, and what its
    # families hold beyond them is dropped.
    name = "a Deep Scan limited to photos finds the photos and nothing else"
    device = filled("vfat")
    sh("mkfs.exfat", "--no-discard", loops.own(device))
    session = session_for(device)
    try:
        result = do(dict(ask("recover_scan", device), session=session,
                         deleted=False, content=True,
                         kinds=diskrecover.families(["Pictures"])))
        found = diskrecover.keep_kinds(
            diskrecover.scan_entries(result, session), ["Pictures"])
        kinds = sorted({e.category for e in found})
        digests = {digest(e.file) for e in found}
        photos = [p for p in wanted if p.endswith(".png")]
        whole = sum(1 for p in photos if wanted[p] in digests)
        check("%s (%d found: %s)" % (name, len(found), ", ".join(kinds)),
              kinds == ["Pictures"] and whole == len(photos),
              "kinds %r, %d of %d photos whole" % (kinds, whole,
                                                   len(photos)))
    except diskctl.HelperError as exc:
        failed(name, str(exc))
    loops.detach(device)

    # ---- after a format of an NTFS drive: names and folders ---------------------
    heading("An NTFS drive that was formatted")
    # The two drivers write their records in two layouts (ntfs3's does not
    # state each record's number); one of each.
    for again, argv, writer in (
            ("NTFS", ["mkfs.ntfs", "--quick", "--force", "--label", "Fresh"],
             "ntfs3"),
            ("exFAT", ["mkfs.exfat", "--no-discard"], fuse)):
        name = ("formatted as %s, an NTFS drive written by %s gives its "
                "files back in their folders" % (again, writer))
        if writer is None:
            skipped(name, "ntfs-3g is not installed")
            continue
        device = filled("ntfs", kind=writer)
        if device is None:
            skipped(name, "mkfs.ntfs is missing")
            continue
        sh(*argv, loops.own(device))
        sh("udevadm", "settle")
        session = session_for(device)
        problems = []
        try:
            result = do(dict(ask("recover_scan", device), session=session,
                             deleted=False, records=True))
            entries = diskrecover.scan_entries(result, session)
            listed = {e.path: e for e in entries}
            # A folder whose own record the new filesystem's table was
            # written over cannot be named any more; its files are then
            # under "Folder unknown" - by name, and whole.
            placed = {}
            for path in wanted:
                parts = path.split("/")
                for tail in ["/".join(parts[k:]) for k in
                             range(1, len(parts))]:
                    if "Before the format/Folder unknown/" + tail in listed:
                        placed[path] = "Folder unknown/" + tail
                if "Before the format/" + path in listed:
                    placed[path] = path
                if path not in placed:
                    problems.append("%s is not listed" % path)
            unknown = sum(1 for p in placed.values()
                          if p.startswith("Folder unknown/"))
            for entry in entries:
                entry.chosen = True
            count = diskrecover.write_selection(session, entries)
            if caller is not None:
                os.chown(os.path.join(session, "selection.json"), caller, -1)
            outcome = do(dict(ask("recover_files", device), session=session,
                              target=os.path.join(session, "recovered")))
            if outcome.get("recovered") != count or outcome.get("failed"):
                problems.append("recovered %r of %d" % (
                    outcome.get("recovered"), count))
            for path, want in wanted.items():
                if path not in placed:
                    continue
                target = os.path.join(session, "recovered",
                                      "Before the format", placed[path])
                if not os.path.exists(target):
                    problems.append("%s was not written" % path)
                elif digest(target) != want:
                    problems.append("%s differs from the original" % path)
            check("%s, byte for byte (%d files, %d under 'Folder unknown' "
                  "as the new table covers their folder)"
                  % (name, len(entries), unknown),
                  not problems, "; ".join(problems[:6]))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
        loops.detach(device)

    # ---- after a format of a FAT32 or exFAT drive: names in folders --------------
    heading("A FAT32 or exFAT drive that was formatted")
    # The old top folder goes with any format: what was in it (video.bin)
    # and the names of the folders it held. Everything inside a folder is
    # expected back under its own name, in its own sub-folders.
    inside = [p for p in wanted if "/" in p]
    for before, first, again, argv in (
            ("FAT32", ["mkfs.fat", "-F", "32"], "FAT32",
             ["mkfs.fat", "-F", "32"]),
            ("FAT32", ["mkfs.fat", "-F", "32"], "exFAT",
             ["mkfs.exfat", "--no-discard"]),
            ("exFAT", ["mkfs.exfat", "--no-discard"], "exFAT",
             ["mkfs.exfat", "--no-discard"]),
            ("exFAT", ["mkfs.exfat", "--no-discard"], "NTFS",
             ["mkfs.ntfs", "--quick", "--force"])):
        name = ("formatted as %s, %s %s drive's files in folders come back "
                "by name" % (again, "an" if before[0].upper() in "AEIOU" else "a",
                             before))
        if not shutil.which(first[0]) or not shutil.which(argv[0]):
            skipped(name, "%s or %s is missing" % (first[0], argv[0]))
            continue
        device = loops.create(160 * MIB)
        sh(*first, loops.own(device))
        point = loops.mount(device)
        sh("cp", "-r", *[os.path.join(source, n)
                         for n in os.listdir(source)], point)
        sh("sync")
        loops.unmount(point)
        sh(*argv, loops.own(device))
        sh("udevadm", "settle")
        session = session_for(device)
        problems = []
        try:
            result = do(dict(ask("recover_scan", device), session=session,
                             deleted=False, records=True))
            entries = diskrecover.scan_entries(result, session)
            prefix = "Before the format/"
            by_tail = {}
            for entry in entries:
                # Strip the unnamed top folder: "<prefix><top>/<rest>".
                rest = entry.path[len(prefix):].split("/", 1)
                if len(rest) == 2:
                    by_tail[rest[1]] = entry
            found = {}
            for path in inside:
                tail = path.split("/", 1)[1]
                if tail in by_tail:
                    found[path] = by_tail[tail]
                else:
                    problems.append("%s is not listed" % path)
            stray = [e.path for e in entries
                     if os.path.basename(e.path) == "video.bin"]
            for entry in entries:
                entry.chosen = entry in found.values()
            count = diskrecover.write_selection(session, entries)
            if caller is not None:
                os.chown(os.path.join(session, "selection.json"), caller, -1)
            if count:
                outcome = do(dict(ask("recover_files", device),
                                  session=session, target=os.path.join(
                                      session, "recovered")))
                if outcome.get("recovered") != count:
                    problems.append("recovered %r of %d" % (
                        outcome.get("recovered"), count))
            for path, entry in found.items():
                target = os.path.join(session, "recovered", entry.path)
                if not os.path.exists(target):
                    problems.append("%s was not written" % path)
                elif digest(target) != wanted[path]:
                    problems.append("%s differs from the original" % path)
            check("%s, byte for byte (%d of %d; %d listed in all%s)" % (
                name, len(found), len(inside), len(entries),
                "; the top folder's files are gone, as expected"
                if not stray else "; the old top folder survived too"),
                not problems,
                "; ".join(problems[:6]) + " | listed: %r"
                % sorted(e.path for e in entries)[:10])
        except diskctl.HelperError as exc:
            failed(name, str(exc))
        loops.detach(device)

    big = loops.create(6000 * MIB)
    with open(loops.own(big), "r+b") as handle:
        handle.write(os.urandom(64 * MIB))
    session = session_for(big)

    def stop_on_progress(event):
        if event.get("event") == "progress":
            client.cancel()

    try:
        do(dict(ask("recover_scan", big), session=session, content=True),
           on_event=stop_on_progress)
        failed("a search can be stopped", "it ran to the end")
    except diskctl.Cancelled as exc:
        check("a search can be stopped and keeps what it had found",
              "is kept" in str(exc), str(exc))
    except diskctl.HelperError as exc:
        failed("a search can be stopped", str(exc))
    loops.detach(big)
    shutil.rmtree(work, ignore_errors=True)


def check_rescue(loops, client, caller):
    """A failing drive copied to an image, and the image opened as a drive."""
    do = runner(loops, client)
    if shutil.which("ddrescue") is None or shutil.which("dmsetup") is None:
        skipped("rescuing a failing drive", "ddrescue or dmsetup is missing")
        return
    heading("Rescuing a failing drive")
    work = tempfile.mkdtemp(prefix="rescue-", dir=loops.directory)
    os.chmod(loops.directory, 0o711)
    if caller is not None:
        os.chown(work, caller, -1)
    source = loops.create(200 * MIB)
    sh("sfdisk", "--quiet", source, stdin="label: gpt\nsize=150MiB, type=L\n")
    sh("udevadm", "settle")
    sh("mkfs.ext4", "-q", "-F", "-L", "Rescued", loops.own(source + "p1"))
    kept = populate(loops, source + "p1")
    with open(loops.own(source), "r+b") as handle:
        handle.seek(160 * MIB)
        handle.write(os.urandom(30 * MIB))
    sh("sync")
    bad_sector = 170 * MIB // 512
    bad = loops.with_bad_block(source, bad_sector)
    image = os.path.join(work, "failing.img")
    try:
        result = do(dict(ask("rescue_image", bad), path=image))
        problems = []
        if result.get("missing") != 4096:
            problems.append("%r bytes reported unread, expected 4096"
                            % result.get("missing"))
        with open(source, "rb") as a, open(image, "rb") as b:
            one, two = a.read(), b.read()
        hole = bad_sector * 512
        if not (one[:hole] == two[:hole]
                and two[hole:hole + 4096] == b"\0" * 4096
                and one[hole + 4096:] == two[hole + 4096:]):
            problems.append("the image differs outside the unreadable block")
        if not os.path.exists(image + ".map"):
            problems.append("no map file")
        if caller is not None and (os.stat(image).st_uid != caller
                                   or os.stat(image + ".map").st_uid
                                   != caller):
            problems.append("the files do not belong to the user")
        check("everything readable is rescued, the unreadable block is "
              "zeros and is recorded", not problems, "; ".join(problems))
    except diskctl.HelperError as exc:
        failed("rescuing a drive with a bad block", str(exc))
    try:
        do(dict(ask("rescue_image", bad), path=image))
        failed("an existing image is not overwritten", "it was")
    except diskctl.HelperError as exc:
        check("an existing image is not overwritten", "already exists"
              in str(exc), str(exc))
    loops.unmap(bad)

    # Stopped partway, then carried on with the same file.
    big = loops.create(1500 * MIB)
    with open(loops.own(big), "r+b") as handle:
        handle.write(os.urandom(1500 * MIB))
    second = os.path.join(work, "big.img")

    def stop_on_progress(event):
        if event.get("event") == "progress" and event.get("done", 0) > 0:
            client.cancel()

    try:
        do(dict(ask("rescue_image", big), path=second),
           on_event=stop_on_progress)
        failed("a rescue can be stopped", "it ran to the end")
    except diskctl.Cancelled as exc:
        partial = os.path.exists(second + ".map")
        try:
            result = do(dict(ask("rescue_image", big), path=second,
                             resume=True))
            check("a stopped rescue is carried on from its map and ends "
                  "complete", partial and result.get("missing") == 0
                  and sha(second) == sha(big), "%s / %r" % (exc, result))
        except diskctl.HelperError as again:
            failed("carrying on a stopped rescue", str(again))
    except diskctl.HelperError as exc:
        failed("a rescue can be stopped", str(exc))
    loops.detach(big)
    os.remove(second)

    # The image, opened as a drive.
    heading("An image opened as a drive")
    try:
        result = client.run({"op": "image_attach", "path": image})
        device = result["device"]
        loops.devices[device] = image
        sh("udevadm", "settle")
        read_only = sh("blockdev", "--getro", device).stdout.strip() == "1"
        check("an image appears as a read-only drive with its partitions",
              read_only and os.path.exists(device + "p1")
              and probe(device + "p1").get("LABEL") == "Rescued"
              and file_digest(loops, device + "p1") == kept, repr(result))
        listed = {d.name: d for d in disks.inventory()}
        shown = listed.get(os.path.basename(device))
        check("the application lists it as a disk image, named after its "
              "file", shown is not None and shown.kind == disks.IMAGE
              and shown.title == "failing.img" and shown.read_only,
              repr(shown)[:200])
        do(ask("image_detach", device))
        sh("udevadm", "settle")
        gone = sh("losetup", "--list", "--noheadings", "--output", "NAME",
                  check_code=False).stdout.split()
        check("closing it takes it away and leaves the file",
              device not in gone and os.path.exists(image))
        loops.devices.pop(device, None)
    except diskctl.HelperError as exc:
        failed("an image opened as a drive", str(exc))
    loops.detach(source)
    shutil.rmtree(work, ignore_errors=True)


def check_lost(loops, client, caller):
    """Partitions whose table entries are gone, found and put back."""
    do = runner(loops, client)
    if shutil.which("testdisk") is None:
        skipped("lost partitions", "testdisk is not installed")
        return
    heading("Lost partitions")

    def laid_out():
        disk = loops.create(400 * MIB)
        sh("sfdisk", "--quiet", disk, stdin=(
            "label: gpt\nstart=1MiB, size=120MiB, type=L\n"
            "start=150MiB, size=100MiB, type=EBD0A0A2-B9E5-4433-87C0-"
            "68B6B72699C7\nstart=260MiB, size=100MiB, type=EBD0A0A2-B9E5-"
            "4433-87C0-68B6B72699C7\n"))
        sh("udevadm", "settle")
        sh("mkfs.ext4", "-q", "-F", "-L", "LinuxData", loops.own(disk + "p1"))
        sh("mkfs.ntfs", "-q", "-F", "-f", "-L", "WinData",
           loops.own(disk + "p2"))
        sh("mkfs.exfat", "-L", "CARD", loops.own(disk + "p3"))
        kept = {n: populate(loops, "%sp%d" % (disk, n)) for n in (1, 2, 3)}
        return disk, kept, partitions_of(disk)

    wanted = [(MIB, 120 * MIB, "ext4", "LinuxData"),
              (150 * MIB, 100 * MIB, "ntfs", "WinData"),
              (260 * MIB, 100 * MIB, "exfat", "CARD")]

    # The whole table gone, both copies.
    disk, kept, before = laid_out()
    with open(loops.own(disk), "r+b") as handle:
        handle.write(b"\0" * MIB)
        handle.seek(399 * MIB)
        handle.write(b"\0" * MIB)
    sh("partprobe", disk, check_code=False)
    sh("udevadm", "settle")
    try:
        result = do(ask("partition_search", disk))
        found = [(f["start"], f["size"], f["fstype"], f["label"])
                 for f in result["found"]]
        check("with the table wiped, every partition is found where it "
              "was, and nothing that is not one", found == wanted,
              repr(result))
        for entry in result["found"]:
            do(ask("partition_restore", disk, start=entry["start"],
                   size=entry["size"]))
        sh("udevadm", "settle")
        problems = []
        now = [(s, n) for s, n, _t, _u in partitions_of(disk)]
        if now != [(s, n) for s, n, _t, _u in before]:
            problems.append("the table is %r" % now)
        if gpt_sound(disk) is False:
            problems.append("the GPT is not sound")
        for number in (1, 2, 3):
            part = "%sp%d" % (disk, number)
            if not os.path.exists(part) or file_digest(loops, part) \
                    != kept[number]:
                problems.append("partition %d: its file is not intact"
                                % number)
        check("put back, the partitions are where they were with their "
              "files intact", not problems, "; ".join(problems))
    except diskctl.HelperError as exc:
        failed("finding and restoring lost partitions", str(exc))
    loops.detach(disk)

    # One entry deleted from a table that is otherwise fine.
    disk, kept, before = laid_out()
    sh("sfdisk", "--quiet", "--delete", disk, "2")
    sh("partprobe", disk, check_code=False)
    sh("udevadm", "settle")
    try:
        result = do(ask("partition_search", disk))
        found = [(f["start"], f["size"], f["fstype"], f["label"])
                 for f in result["found"]]
        check("with one entry deleted, only that partition is offered",
              found == [wanted[1]], repr(result))
        if found:
            restored = do(ask("partition_restore", disk, start=found[0][0],
                              size=found[0][1]))
            sh("udevadm", "settle")
            check("it is put back beside the others, which are untouched",
                  file_digest(loops, restored["device"]) == kept[2]
                  and file_digest(loops, disk + "p1") == kept[1]
                  and file_digest(loops, disk + "p3") == kept[3]
                  and len(partitions_of(disk)) == 3, repr(restored))
        try:
            do(ask("partition_restore", disk, start=130 * MIB,
                   size=10 * MIB))
            failed("a place where no filesystem starts is refused",
                   "it was accepted")
        except diskctl.HelperError as exc:
            check("a place where no filesystem starts is refused",
                  "No filesystem begins there" in str(exc), str(exc))
    except diskctl.HelperError as exc:
        failed("restoring one lost partition", str(exc))
    loops.detach(disk)


def check_raid(loops, client, caller):
    """Arrays whose own record of themselves is intact: found, and put
    together read-only, whole or with a member missing."""
    do = runner(loops, client)
    if shutil.which("mdadm") is None:
        skipped("RAID arrays", "mdadm is not installed")
        return
    heading("RAID arrays")
    tag = "kss%d" % os.getpid()

    def built(level, count):
        members = [loops.create(200 * MIB) for _ in range(count)]
        name = "/dev/md/%s-%s" % (tag, level)
        sh("mdadm", "--create", name, "--run", "--quiet", "--level", level,
           "--raid-devices", str(count), "--metadata", "1.2",
           "--homehost", "any", *[loops.own(m) for m in members])
        sh("udevadm", "settle")
        node = os.path.realpath(name)
        sh("mkfs.ext4", "-q", "-F", "-L", "Array" + level, node)
        point = tempfile.mkdtemp(prefix="mnt-", dir=loops.directory)
        sh("mount", node, point)
        with open(os.path.join(point, "data.bin"), "wb") as handle:
            handle.write(os.urandom(4 * MIB))
        kept = digest(os.path.join(point, "data.bin"))
        sh("umount", point)
        os.rmdir(point)
        sh("mdadm", "--wait", node, check_code=False)
        sh("mdadm", "--stop", node)
        sh("udevadm", "settle")
        return members, kept

    def stopped(node):
        subprocess.run(["mdadm", "--stop", node], stderr=subprocess.DEVNULL)
        sh("udevadm", "settle")

    def opened(uuid):
        result = client.run({"op": "raid_open", "uuid": uuid})
        sh("udevadm", "settle")
        return result

    def read_file(node):
        point = tempfile.mkdtemp(prefix="mnt-", dir=loops.directory)
        try:
            sh("mount", "-o", "ro", node, point)
            try:
                return digest(os.path.join(point, "data.bin"))
            finally:
                sh("umount", point)
        finally:
            os.rmdir(point)

    arrays = {}
    for level, count in (("1", 2), ("5", 3)):
        arrays[level] = built(level, count)
    try:
        found = client.run({"op": "raid_scan"})["arrays"]
        mine = {a["level"]: a for a in found
                if any(m in loops.devices for m in a["members"])}
        check("arrays are found on their members, with level and size",
              sorted(mine) == ["raid1", "raid5"]
              and mine["raid1"]["devices"] == 2
              and mine["raid5"]["devices"] == 3
              and not mine["raid5"]["device"], repr(found)[:400])
        for level in ("1", "5"):
            members, kept = arrays[level]
            result = opened(mine["raid" + level]["uuid"])
            node = result["device"]
            state = open("/sys/class/block/%s/md/array_state"
                         % os.path.basename(node)).read().strip()
            listed = {d.name: d for d in disks.inventory()}
            shown = listed.get(os.path.basename(node))
            check("a RAID %s is put together read-only and its files are "
                  "there" % level, read_file(node) == kept
                  and state in ("readonly", "read-auto")
                  and shown is not None and shown.kind == disks.RAID,
                  "%r state=%s shown=%r" % (result, state, shown))
            loops.mapped[node] = None            # ours, for the guard
            do(ask("raid_close", node))
            loops.mapped.pop(node, None)
            check("closing a RAID %s stops it" % level,
                  not os.path.exists("/sys/class/block/%s/md"
                                     % os.path.basename(node)))
        # RAID 5 with one of its three drives gone.
        members, kept = arrays["5"]
        gone = members.pop()
        loops.detach(gone)
        sh("udevadm", "settle")
        result = opened(mine["raid5"]["uuid"])
        check("a RAID 5 missing one drive is still put together, and its "
              "files are whole", read_file(result["device"]) == kept
              and result["present"] == 2, repr(result))
        stopped(result["device"])
        # And with two gone, it says why not.
        loops.detach(members.pop())
        sh("udevadm", "settle")
        try:
            result = opened(mine["raid5"]["uuid"])
            stopped(result["device"])
            failed("a RAID 5 missing two drives is refused",
                   "it was put together: %r" % result)
        except diskctl.HelperError as exc:
            check("a RAID 5 missing two drives is refused, with the reason",
                  "missing" in str(exc), str(exc))
    except diskctl.HelperError as exc:
        failed("RAID arrays", str(exc))
    finally:
        for name in os.listdir("/dev/md") if os.path.isdir("/dev/md") else ():
            if name.startswith(tag):
                stopped(os.path.realpath(os.path.join("/dev/md", name)))
        for members, _kept in arrays.values():
            for member in members:
                subprocess.run(["mdadm", "--zero-superblock", member],
                               stderr=subprocess.DEVNULL)
                loops.detach(member)


def check_guess(loops, client, caller):
    """Arrays whose own record is gone: how their drives fit together is
    worked out from what is on them, they are put together read-only by
    the kernel, their files read back whole, and not a byte of any drive
    changes."""
    do = runner(loops, client)
    for tool in ("mdadm", "dmsetup"):
        if shutil.which(tool) is None:
            skipped("arrays without their record", "%s is missing" % tool)
            return
    heading("Arrays whose own record is gone")
    tag = "kssg%d" % os.getpid()

    def fill(point, count, seed):
        import random
        rng = random.Random(seed)
        kept = {}
        for number in range(count):
            folder = os.path.join(point, "D%03d" % (number % 40))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "f%04d.bin" % number)
            with open(path, "wb") as handle:
                handle.write(rng.randbytes(rng.randrange(500, 40000)))
            kept[os.path.relpath(path, point)] = digest(path)
        return kept

    def made_array(level, count, chunk_kib, layout, mkfs, kind, files,
                   build=False):
        members = [loops.create(160 * MIB) for _ in range(count)]
        name = "/dev/md/%s-%s" % (tag, level)
        if build:
            sh("mdadm", "--build", name, "--level", level, "--raid-devices",
               str(count), "--chunk", str(chunk_kib),
               *[loops.own(m) for m in members])
        else:
            sh("mdadm", "--create", name, "--run", "--quiet", "--level",
               level, "--raid-devices", str(count), "--chunk",
               str(chunk_kib), "--layout", layout, "--metadata", "1.2",
               "--assume-clean", "--homehost", "any",
               *[loops.own(m) for m in members])
        sh("udevadm", "settle")
        node = os.path.realpath(name)
        sh(*mkfs, node)
        point = tempfile.mkdtemp(prefix="mnt-", dir=loops.directory)
        sh("mount", *(["-t", kind] if kind else []), node, point)
        kept = fill(point, files, count)
        sh("umount", point)
        os.rmdir(point)
        offset = 0
        if not build:
            examined = sh("mdadm", "--examine", members[0]).stdout
            found = re.search(r"Data Offset : (\d+) sectors", examined)
            offset = int(found.group(1)) * 512 if found else -1
        sh("mdadm", "--stop", node)
        sh("udevadm", "settle")
        if not build:
            for member in members:
                sh("mdadm", "--zero-superblock", loops.own(member))
        return members, kept, offset

    def contents(node, kept):
        point = tempfile.mkdtemp(prefix="mnt-", dir=loops.directory)
        try:
            sh("mount", "-o", "ro", node, point)
            try:
                return sum(1 for path, want in kept.items()
                           if digest(os.path.join(point, path)) == want)
            finally:
                sh("umount", point)
        finally:
            os.rmdir(point)

    import re
    cases = [
        ("RAID 0 of 3 drives, FAT32, no record at all", "0", 3, 64,
         "raid0", ["mkfs.fat", "-F", "32"], None, 400, True, "raid0"),
        ("RAID 5 of 3 drives, NTFS, record erased", "5", 3, 128,
         "left-symmetric", ["mkfs.ntfs", "--quick", "--force"], "ntfs-3g",
         3000, False, "raid5_ls"),
        # Small clusters, so that on these small drives the allocation
        # bitmap spans many pieces, as it does on any drive of real size.
        ("RAID 5 of 4 drives, exFAT, record erased", "5", 4, 16,
         "right-asymmetric", ["mkfs.exfat", "--no-discard", "-c", "512"],
         None, 400, False, "raid5_ra"),
    ]
    for (title, level, count, chunk_kib, layout, mkfs, kind, files, build,
         expect) in cases:
        if not shutil.which(mkfs[0]) or (kind and not shutil.which(
                "mount." + kind)):
            skipped(title, "%s is missing" % (kind or mkfs[0]))
            continue
        members = []
        try:
            members, kept, offset = made_array(level, count, chunk_kib,
                                               layout, mkfs, kind, files,
                                               build)
            before = [sha(m) for m in members]
            named = [{"device": m, "identity": disks.identity(
                os.path.basename(m))} for m in members]
            shuffled = named[1:] + named[:1]
            for m in members:
                loops.own(m)
            tries = [("", shuffled, None)]
            if count >= 3 and level == "5":
                tries.append((" with a drive missing",
                              shuffled[:1] + [None] + shuffled[2:], 1))
            for extra, given, gone in tries:
                name = "%s%s: found, opened read-only, files whole" % (
                    title, extra)
                result = client.run({"op": "raid_guess", "members": given,
                                     "level": "raid5" if gone else "auto"})
                best = (result.get("candidates") or [{}])[0]
                want_order = [m if gone is None or m != members[
                    (gone + 1) % count] else None for m in members]
                problems = []
                if not result.get("sure"):
                    problems.append("not sure: %s [%s]" % (
                        result.get("why"), " | ".join(
                            "%s %d %d %s %.4f" % (
                                c["layout"], c["chunk"], c["offset"],
                                [os.path.basename(p or "-")
                                 for p in c["order"]], c["score"])
                            for c in result.get("candidates", [])[:4])))
                got = (best.get("layout"), best.get("chunk"),
                       best.get("offset"))
                if got != (expect, chunk_kib * 1024, offset):
                    problems.append("found %r, made %r" % (
                        got, (expect, chunk_kib * 1024, offset)))
                if best.get("order") != want_order:
                    problems.append("order %r, made %r"
                                    % (best.get("order"), want_order))
                whole = 0
                if not problems:
                    opened = client.run({
                        "op": "raid_build", "layout": best["layout"],
                        "chunk": best["chunk"], "offset": best["offset"],
                        "members": [None if p is None else {
                            "device": p, "identity": disks.identity(
                                os.path.basename(p))}
                            for p in best["order"]]})
                    sh("udevadm", "settle")
                    node = opened["device"]
                    try:
                        whole = contents(node, kept)
                    except RuntimeError as exc:
                        problems.append(str(exc)[-200:])
                    listed = {d.name: d for d in disks.inventory()}
                    shown = listed.get(os.path.basename(node))
                    if shown is None or shown.kind != disks.RAID:
                        problems.append("not listed as an array: %r"
                                        % shown)
                    loops.mapped[node] = None
                    do(ask("raid_close", node))
                    loops.mapped.pop(node, None)
                    if os.path.exists(node):
                        problems.append("still there after closing")
                    if whole != len(kept):
                        problems.append("%d of %d files whole"
                                        % (whole, len(kept)))
                check("%s (%d files, %.1f%% of %d checks)" % (
                    name, whole, 100 * (best.get("score") or 0),
                    best.get("checks") or 0), not problems,
                    "; ".join(problems))
            check("%s: not a byte of any drive changed" % title,
                  [sha(m) for m in members] == before)
        except diskctl.HelperError as exc:
            failed(title, str(exc))
        finally:
            for name in os.listdir("/dev/mapper"):
                if name.startswith("kss-raid"):
                    subprocess.run(["dmsetup", "remove", name],
                                   stderr=subprocess.DEVNULL)
            for name in (os.listdir("/dev/md") if os.path.isdir("/dev/md")
                         else ()):
                if name.startswith(tag):
                    subprocess.run(["mdadm", "--stop", os.path.join(
                        "/dev/md", name)], stderr=subprocess.DEVNULL)
            for member in members:
                loops.detach(member)


#: The type to mount a filesystem just made with: probing a fresh one, mount
#: may not recognise it, and a driver not yet loaded is never guessed.
MOUNT_TYPES = {"ext4": "ext4", "btrfs": "btrfs", "xfs": "xfs",
               "ntfs": "ntfs3", "f2fs": "f2fs"}


def fs_bytes(loops, device, fstype):
    """The size a filesystem says it has, mounted (swap: from its header)."""
    if fstype == "swap":
        with open(device, "rb") as handle:
            header = handle.read(4096)
        # The last usable page number, after the 1 KiB boot block.
        last = int.from_bytes(header[1028:1032], "little")
        return (last + 1) * 4096
    point = loops.mount(device, "ro", MOUNT_TYPES.get(fstype))
    try:
        found = os.statvfs(point)
        return found.f_blocks * found.f_frsize
    finally:
        loops.unmount(point)


def check_grow(loops, client, caller):
    do = runner(loops, client)
    heading("Cloning onto a larger drive and using the free space")
    makers = {
        "ext4": ["mkfs.ext4", "-q", "-F"],
        "btrfs": ["mkfs.btrfs", "-q", "-f"],
        "xfs": ["mkfs.xfs", "-q", "-f"],
        "ntfs": ["mkfs.ntfs", "--quick", "--force"],
        "f2fs": ["mkfs.f2fs", "-q", "-f"],
        "swap": ["mkswap", "-q"],
    }
    cases = [(fs, "gpt", "exact") for fs in makers]
    cases += [("ext4", "dos", "exact"), ("ext4", "gpt", "smart"),
              ("ntfs", "dos", "smart")]
    for fstype, label, mode in cases:
        name = "%s on %s, %s copy, grown into the free space" % (
            fstype, "GPT" if label == "gpt" else "MBR", mode)
        if shutil.which(makers[fstype][0]) is None:
            skipped(name, "%s is not installed" % makers[fstype][0])
            continue
        source = loops.create(400 * MIB)
        efi = ("C12A7328-F81F-11D2-BA4B-00A0C93EC93B" if label == "gpt"
               else "ef")
        last = "L" if fstype != "ntfs" else (
            "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7" if label == "gpt"
            else "7")
        sh("sfdisk", "--quiet", source,
           stdin="label: %s\nsize=40MiB, type=%s\nsize=330MiB, type=%s\n"
                 % (label, efi, last))
        sh("udevadm", "settle")
        sh("mkfs.fat", "-n", "EFI", loops.own(source + "p1"))
        sh(*makers[fstype], loops.own(source + "p2"))
        kind = MOUNT_TYPES.get(fstype)
        kept = None if fstype == "swap" else populate(loops, source + "p2",
                                                      kind=kind)
        sh("sync")
        before = fs_bytes(loops, source + "p2", fstype)
        target = loops.create(700 * MIB, fill=b"\x55")
        try:
            result = do(clone_request(source, target, mode=mode, grow=True))
            sh("udevadm", "settle")
            problems = []
            grown = result.get("grown") or {}
            if not grown:
                problems.append("not grown: %r"
                                % result.get("grow_skipped"))
            parts = partitions_of(target)
            size = parts[1][1] * 512 if len(parts) > 1 else 0
            if size < 600 * MIB:
                problems.append("the partition is %d MiB" % (size // MIB))
            if parts[:1] != partitions_of(source)[:1]:
                problems.append("the first partition changed")
            after = fs_bytes(loops, target + "p2", fstype)
            if after < before + 200 * MIB:
                problems.append("the filesystem is %d MiB, was %d"
                                % (after // MIB, before // MIB))
            if probe(source + "p2").get("UUID") != probe(
                    target + "p2").get("UUID"):
                problems.append("the filesystem's ID changed")
            if fstype != "swap":
                if fsck_clean(fstype, target + "p2") is False:
                    problems.append("fsck reports errors")
                if file_digest(loops, target + "p2", kind=kind) != kept:
                    problems.append("a file on the copy differs")
            if label == "gpt" and gpt_sound(target) is False:
                problems.append("the GPT is not sound")
            check(name, not problems, "; ".join(problems))
        except diskctl.HelperError as exc:
            failed(name, str(exc))
        loops.detach(target)
        loops.detach(source)

    # A last partition that is not grown: the copy is kept and says why.
    name = "a FAT last partition is left as it is, and the copy says why"
    source = loops.create(200 * MIB)
    sh("sfdisk", "--quiet", source, stdin="label: gpt\nsize=150MiB, "
       "type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n")
    sh("udevadm", "settle")
    sh("mkfs.fat", "-n", "DATA", loops.own(source + "p1"))
    target = loops.create(300 * MIB)
    try:
        result = do(clone_request(source, target, grow=True))
        check(name, result.get("verified") and "FAT" in (
            result.get("grow_skipped") or "") and not result.get("grown"),
            repr(result))
    except diskctl.HelperError as exc:
        failed(name, str(exc))
    loops.detach(target)
    loops.detach(source)


def check_stick(loops, client, caller):
    """The recovery stick, written onto a loop device. KSS_STICK_IMAGE (or
    --keep-stick=PATH, which pkexec lets through): where to keep a copy of
    the result, to start it in a virtual machine."""
    from ksyssupervisor import rescuebuild
    do = runner(loops, client)
    heading("The recovery stick")
    stage = tempfile.mkdtemp(prefix="stick-", dir=loops.directory)
    try:
        built = rescuebuild.build(stage, settings=rescuebuild.Settings(
            keyboard="us"))
    except rescuebuild.BuildError as exc:
        failed("the recovery system is put together", str(exc))
        return
    if caller is not None:
        # Made by the user in real use: the helper reads them as the user,
        # and copies only their files.
        os.chmod(loops.directory, 0o711)
        os.chown(stage, caller, -1)
        for name in os.listdir(stage):
            os.chown(os.path.join(stage, name), caller, -1)
    with open(built.manifest) as handle:
        manifest = json.load(handle)
    stick = loops.create(1536 * MIB, fill=b"\x5a")
    request = {"op": "rescue_write", "device": stick,
               "identity": disks.identity(os.path.basename(stick)),
               "stage": stage, "files": manifest["files"], "verify": True}
    try:
        result = do(request)
    except diskctl.HelperError as exc:
        failed("a stick is written and read back", str(exc))
        return
    sh("udevadm", "settle")
    problems = []
    layout = table(stick)
    parts = layout.get("partitions", [])
    if layout.get("label") != "dos" or len(parts) != 1:
        problems.append("table %r" % layout)
    elif parts[0].get("type") != "c" or not parts[0].get("bootable"):
        problems.append("partition %r" % parts[0])
    found = probe(stick + "p1")
    if (found.get("TYPE"), found.get("LABEL")) != ("vfat", "KSSRESCUE"):
        problems.append("filesystem %r" % found)
    if found.get("UUID") != result.get("uuid"):
        problems.append("ID %r, result %r" % (found.get("UUID"), result))
    point = loops.mount(stick + "p1", "ro", "vfat")
    try:
        for name, want in manifest["files"].items():
            path = os.path.join(point, "kss", name)
            if not os.path.isfile(path) or digest(path) != want["sha256"]:
                problems.append("kss/%s differs" % name)
        boot = os.path.join(point, "EFI", "BOOT")
        listed = sorted(os.listdir(boot)) if os.path.isdir(boot) else []
        if "BOOTX64.EFI" not in listed or "grub.cfg" not in listed:
            problems.append("EFI/BOOT holds %r" % listed)
        if any(n.lower().startswith("fb") for n in listed):
            problems.append("the fallback loader was copied")
        with open(os.path.join(boot, "grub.cfg")) as handle:
            config = handle.read()
        if "kss.stick=%s" % result.get("uuid") not in config:
            problems.append("grub.cfg does not name the stick")
        for folder in ("state", "logs"):
            if not os.path.isdir(os.path.join(point, "KSysSupervisor",
                                              folder)):
                problems.append("no KSysSupervisor/%s" % folder)
    finally:
        loops.unmount(point)
    check("a stick is written, read back and holds what was built "
          "(%s, Secure Boot %s)" % (rescuebuild.format_size(
              result.get("size", 0)), "on" if result.get("secure_boot")
              else "off"), not problems, "; ".join(problems))
    keep = os.environ.get("KSS_STICK_IMAGE") or next(
        (a.split("=", 1)[1] for a in sys.argv[1:]
         if a.startswith("--keep-stick=")), None)
    if keep:
        shutil.copyfile(loops.devices[stick], keep)
        if caller is not None:
            os.chown(keep, caller, -1)
        print("  stick image kept at %s" % keep)

    # Files that are not the user's are not copied.
    if caller is not None:
        os.chown(os.path.join(stage, "vmlinuz"), 0, -1)
        try:
            do(request)
            failed("a file that is not the user's is refused",
                   "it was copied")
        except diskctl.HelperError as exc:
            check("a file that is not the user's is refused",
                  "does not belong to you" in str(exc), str(exc))
        os.chown(os.path.join(stage, "vmlinuz"), caller, -1)
    os.rename(os.path.join(stage, "vmlinuz"),
              os.path.join(stage, "real-vmlinuz"))
    os.symlink("/etc/shadow", os.path.join(stage, "vmlinuz"))
    try:
        do(request)
        failed("a symlink in the folder is not followed", "it was copied")
    except diskctl.HelperError as exc:
        check("a symlink in the folder is not followed",
              "could not be opened" in str(exc), str(exc))
    small = loops.create(800 * MIB)
    try:
        do(dict(request, device=small,
                identity=disks.identity(os.path.basename(small))))
        failed("a stick under 900 MB is refused", "it was accepted")
    except diskctl.HelperError as exc:
        check("a stick under 900 MB is refused", "too small" in str(exc),
              str(exc))


SECTIONS = {
    "format": check_format,
    "restore": check_restore,
    "maintain": check_maintain,
    "copy": check_copy,
    "grow": check_grow,
    "partitions": check_partitions,
    "recover": check_recover,
    "rescue": check_rescue,
    "lost": check_lost,
    "raid": check_raid,
    "guess": check_guess,
    "stick": check_stick,
}


if __name__ == "__main__":
    sys.exit(main())
