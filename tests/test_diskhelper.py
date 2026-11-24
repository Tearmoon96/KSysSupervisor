"""The disk utility's privileged helper, against a fake block-device tree.

No root, no real disk and no real tool: the helper is loaded as a module,
pointed at a temporary /sys, /dev and /proc, and given a runner that records
what it would have executed. What is asserted is what matters when the real
thing runs as root: which devices it refuses, which commands it would run on
which device in which order, and that it stops when a step fails.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, HELPER, MIB, FakeBlockTree, load_helper  # noqa: E402

from ksyssupervisor import diskfs, diskops, disks  # noqa: E402

#: The option each mkfs takes its label with, to read it back out of argv.
LABEL_FLAGS = {"mkfs.fat": "-n", "mkfs.exfat": "-L", "mkfs.ntfs": "--label",
               "mkfs.ext4": "-L", "mkfs.btrfs": "--label", "mkfs.xfs": "-L",
               "mkfs.f2fs": "-l"}
#: The switch each mkfs takes the cluster size by: bytes, or for mkfs.fat
#: logical sectors.
CLUSTER_FLAGS = {"mkfs.fat": "-s", "mkfs.exfat": "-c",
                 "mkfs.ntfs": "--cluster-size"}
BLKID_NAMES = {"mkfs.fat": "vfat", "mkfs.exfat": "exfat", "mkfs.ntfs": "ntfs",
               "mkfs.ext4": "ext4", "mkfs.btrfs": "btrfs", "mkfs.xfs": "xfs",
               "mkfs.f2fs": "f2fs"}


class Tools:
    """Stands in for every external command the helper runs."""

    def __init__(self, tree):
        self.tree = tree
        self.calls = []          # (tool name, arguments, stdin)
        self.fail = {}           # tool name -> stderr; makes it exit 1
        self.missing = set()
        self.contents = {}       # device name -> blkid export fields
        self.lie = {}            # device name -> fields blkid reports instead
        self.stdout = {}         # tool name -> what it prints
        self.codes = {}          # tool name -> exit status, or a list of
        #                          them for one call after another
        self.lines = {}          # tool name -> lines a long run prints
        self.stop_after = {}     # tool name -> stopped after that many lines
        self.tables = {}         # disk name -> what sfdisk --json says of it
        self.writes = {}         # tool name -> what it writes to its output
        #                          file (or {last argument: bytes})
        self.makes = {}          # tool name -> what it does, given its args
        self.cluster_lie = None  # cluster size mkfs writes instead

    def find(self, *names):
        for name in names:
            if name not in self.missing:
                return "/usr/sbin/" + name
        return None

    def names(self):
        return [name for name, _args, _stdin in self.calls]

    def of(self, name):
        return [args for tool, args, _stdin in self.calls if tool == name]

    def code(self, name):
        code = self.codes.get(name, 0)
        if isinstance(code, list):
            code = code.pop(0) if len(code) > 1 else code[0]
        return code

    def stream(self, argv, on_line, should_stop, timeout=None, cwd=None,
               env=None, stdout=None, on_partial=None):
        """A tool that runs for a while: its lines, then its exit status.

        Lines given as ("partial", text) are ones the tool redraws in place,
        handed to `on_partial`. mkfs runs this way too, and leaves behind
        what run() would have."""
        name, args = os.path.basename(argv[0]), list(argv[1:])
        if name in LABEL_FLAGS:
            code, out, err = self.run(argv)
            for line in (self.lines.get(name, []) + (err or out).splitlines()):
                if isinstance(line, tuple):
                    if on_partial is not None:
                        on_partial(line[1])
                else:
                    on_line(line)
            return code, False
        self.calls.append((name, args, None))
        self.options = {"cwd": cwd, "env": env}
        if stdout is not None and name in self.writes:
            data = self.writes[name]
            if isinstance(data, dict):
                data = data.get(args[-1], b"")
            os.write(stdout, data)
        if name in self.makes:
            self.makes[name](args)
        lines = self.lines.get(name, [])
        if lines and isinstance(lines[0], list):
            lines = lines.pop(0) if len(lines) > 1 else lines[0]
        for index, line in enumerate(lines):
            if self.stop_after.get(name) == index and should_stop():
                return -15, True
            on_line(line)
        code = self.code(name)
        if code == 0 and name in ("e2image", "ntfsclone", "xfs_copy"):
            # A filesystem copied by its own tool: the target now holds
            # what the source holds.
            paths = [a for a in args if a.startswith(self.tree.dev)]
            source, target = paths if name != "ntfsclone" else paths[::-1]
            self.contents[os.path.basename(target)] = dict(
                self.contents.get(os.path.basename(source), {}))
        return code, False

    def _sfdisk_read(self, args, disk):
        import json
        table = self.tables.get(disk)
        if table is None:
            return 1, "", "sfdisk: %s: does not contain a recognized " \
                          "partition table" % disk
        if "--json" in args:
            return 0, json.dumps({"partitiontable": table}), ""
        lines = ["label: %s" % table["label"], "label-id: 1234",
                 "device: /dev/%s" % disk, "unit: sectors",
                 "first-lba: 2048", "last-lba: 99999999", ""]
        for entry in table["partitions"]:
            lines.append("%s : start=%d, size=%d, type=%s" % (
                entry["node"], entry["start"], entry["size"],
                entry["type"]))
        return 0, "\n".join(lines) + "\n", ""

    def _sysfs(self, name, attribute, value):
        path = os.path.join(self.tree.block, name, attribute)
        with open(path, "w") as handle:
            handle.write("%d\n" % value)

    def _sfdisk_edit(self, args, stdin):
        """sfdisk changing one entry of a table kept in self.tables."""
        if "--delete" in args:
            disk = os.path.basename(args[-2])
            number = int(args[-1])
        else:
            disk = os.path.basename(args[-1])
        table = self.tables[disk]
        entries = table["partitions"]
        suffix = "p" if disk[-1].isdigit() else ""

        def numbered(n):
            return "/dev/%s%s%d" % (disk, suffix, n)

        if "--delete" in args:
            table["partitions"] = [e for e in entries
                                   if e["node"] != numbered(number)]
            self.tree.remove("%s%s%d" % (disk, suffix, number))
        elif "--append" in args:
            fields = dict(part.strip().split("=")
                          for part in stdin.strip().split(","))
            used = {int(e["node"].rsplit(disk + suffix, 1)[1])
                    for e in entries}
            number = next(n for n in range(1, 99) if n not in used)
            entries.append({"node": numbered(number),
                            "start": int(fields["start"]),
                            "size": int(fields["size"]),
                            "type": fields["type"]})
            self.tree.add_partition(disk, "%s%s%d" % (disk, suffix, number),
                                    number, int(fields["start"]),
                                    int(fields["size"]) * 512)
        else:
            number = int(args[args.index("-N") + 1])
            start, size = (stdin.strip().split(",") + [""])[:2]
            entry = [e for e in entries if e["node"] == numbered(number)][0]
            name = "%s%s%d" % (disk, suffix, number)
            if start:
                entry["start"] = int(start)
                self._sysfs(name, "start", int(start))
            if size:
                entry["size"] = int(size)
                self._sysfs(name, "size", int(size))
        return 0, "", ""

    def _copy_table(self, disk, script):
        """sfdisk fed another disk's dump: the same partitions appear."""
        for name in os.listdir(self.tree.block):
            if name.startswith(disk) and name != disk:
                self.tree.remove(name)
        self.written_tables = getattr(self, "written_tables", {})
        self.written_tables[disk] = script
        number = 0
        for line in script.splitlines():
            if line.startswith("label:"):
                self.contents[disk] = {"PTTYPE": line.split(":")[1].strip()}
            if " : start=" not in line:
                continue
            number += 1
            fields = dict(part.strip().split("=") for part
                          in line.split(" : ")[1].split(","))
            self.tree.add_partition(disk, "%s%d" % (disk, number), number,
                                    int(fields["start"]),
                                    int(fields["size"]) * 512)

    def run(self, argv, stdin=None, timeout=None):
        name, args = os.path.basename(argv[0]), list(argv[1:])
        self.calls.append((name, args, stdin))
        if name in self.fail:
            return 1, "", self.fail[name]
        if name in self.stdout or name in self.codes:
            return self.code(name), self.stdout.get(name, ""), ""
        device = os.path.basename(args[-1]) if args else ""

        if name == "umount":
            self.tree.unmount(args[-1])
        elif name == "wipefs":
            self.contents.pop(device, None)
        elif name == "sfdisk" and ("--json" in args or "--dump" in args):
            return self._sfdisk_read(args, device)
        elif name == "sfdisk" and ("--append" in args or "-N" in args
                                   or "--delete" in args):
            return self._sfdisk_edit(args, stdin)
        elif name == "sfdisk" and "--relocate" in args:
            pass
        elif name == "sfdisk" and stdin and " : start=" in stdin:
            self._copy_table(device, stdin)
        elif name == "sfdisk" and "--part-type" not in args:
            self._new_table(device, stdin)
        elif name in LABEL_FLAGS:
            fields = {"TYPE": BLKID_NAMES[name], "UUID": "new-uuid"}
            flag = LABEL_FLAGS[name]
            if flag in args:
                fields["LABEL"] = args[args.index(flag) + 1]
            self.contents[device] = fields
            self._write_boot(name, args)
        elif name == "e2label":
            self.contents[os.path.basename(args[0])]["LABEL"] = args[1]
        elif name == "blkid":
            fields = self.lie.get(device, self.contents.get(device)) or {}
            if "--match-tag" not in args:
                # Every tag at once: escaped, as the real blkid prints it.
                return 0, "".join(
                    "%s=%s\n" % (key, value.replace(" ", "\\ "))
                    for key, value in fields.items()), ""
            tag = args[args.index("--match-tag") + 1]
            if tag not in fields:
                return 2, "", ""
            return 0, fields[tag] + "\n", ""
        return 0, "", ""

    def _write_boot(self, name, args):
        """The start of a boot sector, as far as the cluster size goes: what
        the helper reads back to check it got what it asked for."""
        flag = CLUSTER_FLAGS.get(name)
        if flag not in args:
            return
        path = args[-1]
        disk = os.path.basename(os.path.dirname(os.path.realpath(
            os.path.join(self.tree.block, os.path.basename(path)))))
        sector = 512
        for each in (os.path.basename(path), disk):
            try:
                with open(os.path.join(self.tree.block, each, "queue",
                                       "logical_block_size")) as handle:
                    sector = int(handle.read())
                break
            except OSError:
                continue
        value = int(args[args.index(flag) + 1])
        cluster = value * sector if name == "mkfs.fat" else value
        cluster = self.cluster_lie or cluster
        boot = bytearray(512)
        if name == "mkfs.exfat":
            boot[3:11] = b"EXFAT   "
            boot[108] = sector.bit_length() - 1
            boot[109] = (cluster // sector).bit_length() - 1
        elif name == "mkfs.ntfs":
            boot[3:11] = b"NTFS    "
            boot[0x0B:0x0D] = sector.to_bytes(2, "little")
            per = cluster // sector
            boot[0x0D] = per if per <= 0x80 else 256 - (per.bit_length() - 1)
        else:
            boot[11:13] = sector.to_bytes(2, "little")
            boot[13] = cluster // sector
        with open(path, "r+b") as handle:
            handle.write(boot)

    def _new_table(self, disk, script):
        start = None
        for line in script.splitlines():
            if line.startswith("label:"):
                self.contents[disk] = {"PTTYPE": line.split(":")[1].strip()}
            if line.startswith("start="):
                start = int(line.split("=")[1].split(",")[0])
        for name in os.listdir(self.tree.block):
            if name.startswith(disk) and name != disk:
                self.tree.remove(name)
        if disk in self.tables:
            self.tables[disk] = dict(self.tables[disk], partitions=[],
                                     label=self.contents[disk]["PTTYPE"])
        if start is None:
            return                       # an empty table, no partition
        entry = os.path.join(self.tree.block, disk)
        with open(os.path.join(entry, "size")) as handle:
            sectors = int(handle.read())
        with open(os.path.join(entry, "queue",
                               "logical_block_size")) as handle:
            logical = int(handle.read())
        start_512 = start * logical // 512
        suffix = "p1" if disk[-1].isdigit() else "1"
        self.tree.add_partition(disk, disk + suffix, 1, start_512,
                                (sectors - start_512 - 2048) * 512)


class HelperCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(self._remove_tree)
        self.tree = FakeBlockTree(self.root)
        self.module = load_helper()
        self.tree.install_helper(self.module)
        self.tools = Tools(self.tree)
        self.out = io.StringIO()
        self.helper = self.module.Helper(out=self.out, runner=self.tools.run,
                                         tools=self.tools.find,
                                         streamer=self.tools.stream)

        # A system disk, a data disk with a mounted volume, and a USB stick.
        self.tree.add_disk("nvme0n1", 500 * GIB, diskseq=1)
        self.tree.add_partition("nvme0n1", "nvme0n1p1", 1, 2048, 600 * MIB)
        self.tree.add_partition("nvme0n1", "nvme0n1p2", 2, 1230848, 400 * GIB)
        self.tree.mount("nvme0n1p1", "/boot/efi", "vfat")
        self.tree.mount("nvme0n1p2", "/", "ext4")

        self.tree.add_disk("sdb", 1000 * GIB, diskseq=2)
        self.tree.add_partition("sdb", "sdb1", 1, 2048, 999 * GIB)
        self.tree.mount("sdb1", "/mnt/games")
        self.tools.contents["sdb"] = {"PTTYPE": "dos"}
        self.tools.contents["sdb1"] = {"TYPE": "ext4", "LABEL": "Games"}

        self.tree.add_disk("sdd", 8 * GIB, diskseq=7)
        self.tree.add_partition("sdd", "sdd1", 1, 2048, 8 * GIB - 2 * MIB)

    def _remove_tree(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def request(self, name, **fields):
        base = {"op": "format", "id": 1, "device": "/dev/%s" % name,
                "identity": self.tree.identity(name), "fstype": "ext4",
                "label": "", "erase": "none", "table": None}
        # Paths in requests are the real-looking ones; the helper maps them
        # onto its own /dev, which here is the temporary one.
        base["device"] = os.path.join(self.tree.dev, name)
        base.update(fields)
        return base

    def refused(self, request):
        with self.assertRaises(self.module.Refusal) as caught:
            self.helper.OPERATIONS[request["op"]](self.helper, request)
        return str(caught.exception)

    def events(self):
        return [json.loads(line) for line in
                self.out.getvalue().splitlines() if line.strip()]

    def squeezed(self):
        """The tools run, in order, with a run of the same one counted once:
        examining a device takes one blkid call per thing asked about it."""
        names = []
        for name in self.tools.names():
            if not names or names[-1] != name or name != "blkid":
                names.append(name)
        return names

    def destructive(self):
        """Tools run so far that change a device."""
        return [n for n in self.tools.names()
                if n not in ("blkid", "udevadm", "umount")]


class TargetValidationTest(HelperCase):
    def test_only_a_device_under_dev_is_accepted(self):
        for device in ("/etc/passwd", "sdd", "", None, 5,
                       self.tree.dev + "/../etc/passwd",
                       self.tree.dev + "/mapper/x",
                       self.tree.dev + "/sdd/../sdb"):
            request = self.request("sdd", table="gpt")
            request["device"] = device
            self.refused(request)
        self.assertEqual(self.destructive(), [])

    def test_a_device_that_is_gone_is_refused(self):
        request = self.request("sdd", table="gpt")
        self.tree.remove("sdd1")
        self.tree.remove("sdd")
        self.assertIn("unplugged", self.refused(request))

    def test_a_name_that_is_not_a_block_device_is_refused(self):
        os.remove(os.path.join(self.tree.dev, "sdd"))
        self.assertIn("not a block device",
                      self.refused(self.request("sdd", table="gpt")))

    def test_a_request_without_identity_is_refused(self):
        self.assertIn("which device",
                      self.refused(self.request("sdd", table="gpt",
                                                identity=None)))

    def test_a_swapped_stick_is_refused(self):
        """Same name, same size, different disk: only the sequence number
        tells them apart, and that is exactly the case it is there for."""
        request = self.request("sdd", table="gpt")
        with open(os.path.join(self.tree.block, "sdd", "diskseq"), "w") as f:
            f.write("8\n")
        self.assertIn("unplugged", self.refused(request))
        self.assertEqual(self.destructive(), [])

    def test_a_different_size_is_refused(self):
        request = self.request("sdd", table="gpt")
        request["identity"]["size"] += 512
        self.assertIn("size", self.refused(request))

    def test_a_partition_that_moved_is_refused(self):
        request = self.request("sdd1")
        request["identity"]["start"] = 4096
        self.assertIn("start", self.refused(request))

    def test_without_diskseq_size_and_start_still_decide(self):
        os.remove(os.path.join(self.tree.block, "sdd", "diskseq"))
        request = self.request("sdd1")
        self.assertIsNone(request["identity"]["diskseq"])
        self.helper.op_format(request)
        self.assertIn("mkfs.ext4", self.tools.names())

    def test_an_empty_card_reader_is_refused(self):
        self.tree.add_disk("sde", 0, diskseq=9)
        self.assertIn("no media",
                      self.refused(self.request("sde", table="gpt")))

    def test_a_write_protected_device_is_refused(self):
        self.tree.add_disk("sde", GIB, diskseq=9, read_only=1)
        self.assertIn("read-only",
                      self.refused(self.request("sde", table="gpt")))


class SystemGuardTest(HelperCase):
    """Nothing the running system stands on is ever written to."""

    def assert_system(self, name, **fields):
        fields.setdefault("table", None if name[-1].isdigit()
                          and name != "nvme0n1" else "gpt")
        text = self.refused(self.request(name, **fields))
        self.assertIn("running system", text)
        self.assertEqual(self.destructive(), [])
        self.assertNotIn("umount", self.tools.names())
        return text

    def test_the_root_partition(self):
        self.assertIn("/", self.assert_system("nvme0n1p2"))

    def test_the_efi_partition(self):
        self.assertIn("/boot/efi", self.assert_system("nvme0n1p1"))

    def test_the_whole_system_disk(self):
        text = self.assert_system("nvme0n1", table="gpt")
        self.assertIn("/boot/efi", text)

    def test_every_system_mount_point(self):
        for mountpoint in sorted(disks.SYSTEM_MOUNTS) + ["/var/lib/docker",
                                                         "/boot/grub2",
                                                         "/usr/local",
                                                         "/var/home"]:
            self.tree.mount("sdd1", mountpoint)
            self.assert_system("sdd1")
            self.tree.unmount(mountpoint)

    def test_a_users_own_mount_is_not_the_system(self):
        """/var/mnt is where an image-based system puts /mnt, and a test's
        scratch mount under /var/tmp is nobody's system either."""
        for mountpoint in ("/mnt/games", "/run/media/user/STICK",
                           "/var/mnt/data", "/var/tmp/scratch/mnt",
                           "/var/run/media/user/STICK", "/home/user/Games",
                           "/media/usb"):
            self.tools.calls.clear()
            self.tree.mount("sdd1", mountpoint)
            self.assertFalse(self.module.is_system_mount(mountpoint))
            self.assertFalse(disks.is_system_mount(mountpoint))
            self.helper.op_format(self.request("sdd1"))
            self.assertEqual(self.tools.of("umount")[0][-1], mountpoint)

    def test_swap_in_use(self):
        self.tree.swap("sdd1")
        self.assertIn("swap", self.refused(self.request("sdd1")))
        self.assertEqual(self.destructive(), [])

    def test_a_system_volume_inside_an_encrypted_container(self):
        """/home on LUKS: the partition itself is not mounted anywhere."""
        self.tree.add_holder("sdd1", "dm-0")
        self.tree.mount("dm-0", "/home")
        self.assert_system("sdd1")
        self.assert_system("sdd", table="gpt")

    def test_a_second_device_of_the_system_btrfs(self):
        """A Btrfs over two drives is mounted under one device's name."""
        self.tree.add_btrfs("abc", "nvme0n1p2", "sdd1")
        self.tree._mounts = [m for m in self.tree._mounts if m[1] != "/"]
        self.tree.mount("nvme0n1p2", "/", "btrfs")
        self.assert_system("sdd1")

    def test_the_lists_match_the_user_interface(self):
        """The buttons are greyed by the same list the helper refuses by."""
        self.assertEqual(self.module.SYSTEM_MOUNTS, disks.SYSTEM_MOUNTS)
        self.assertEqual(self.module.SYSTEM_PREFIXES, disks.SYSTEM_PREFIXES)

    def test_an_open_container_that_is_not_mounted_is_refused_too(self):
        self.tree.add_holder("sdd1", "dm-3")
        text = self.refused(self.request("sdd1"))
        self.assertIn("storage layer", text)
        self.assertEqual(self.destructive(), [])


class FormatDiskTest(HelperCase):
    def test_a_whole_stick_as_fat32(self):
        result = self.helper.op_format(self.request(
            "sdd", table="dos", fstype="vfat", label="photos"))

        self.assertEqual(self.squeezed(), [
            "wipefs", "wipefs",          # the old partition, then the disk
            "sfdisk", "udevadm",
            "mkfs.fat", "udevadm", "blkid"])
        wiped = [os.path.basename(args[-1]) for args in self.tools.of("wipefs")]
        self.assertEqual(wiped, ["sdd1", "sdd"])

        sfdisk, script = [(a, s) for n, a, s in self.tools.calls
                          if n == "sfdisk"][0]
        self.assertEqual(os.path.basename(sfdisk[-1]), "sdd")
        self.assertEqual(script, "label: dos\nstart=2048, type=c\n")

        mkfs = self.tools.of("mkfs.fat")[0]
        self.assertEqual(os.path.basename(mkfs[-1]), "sdd1")
        self.assertEqual(mkfs[:-1], ["-F", "32", "-n", "PHOTOS"])
        self.assertEqual(result["label"], "PHOTOS")
        self.assertEqual(os.path.basename(result["device"]), "sdd1")

    def test_gpt_uses_the_type_windows_looks_for(self):
        self.helper.op_format(self.request("sdd", table="gpt",
                                           fstype="exfat", label="Data"))
        script = [s for n, _a, s in self.tools.calls if n == "sfdisk"][0]
        self.assertEqual(script, "label: gpt\nstart=2048, type=%s\n"
                         % self.module.GPT_BASIC_DATA)

    def test_a_4k_native_drive_still_starts_at_one_mebibyte(self):
        self.tree.add_disk("sde", 64 * GIB, diskseq=9, logical=4096)
        self.helper.op_format(self.request("sde", table="gpt"))
        script = [s for n, _a, s in self.tools.calls if n == "sfdisk"][0]
        self.assertIn("start=256,", script)

    def test_mounted_volumes_are_unmounted_first(self):
        self.helper.op_format(self.request("sdb", table="gpt"))
        self.assertEqual(self.tools.names()[0], "umount")
        self.assertEqual(self.tools.of("umount")[0][-1], "/mnt/games")
        self.assertNotIn("/mnt/games", self.tree.mounted())

    def test_a_volume_that_will_not_unmount_stops_everything(self):
        self.tools.fail["umount"] = "umount: /mnt/games: target is busy."
        text = self.refused(self.request("sdb", table="gpt"))
        self.assertIn("target is busy", text)
        self.assertEqual(self.destructive(), [])

    def test_every_tool_is_found_before_anything_is_erased(self):
        for missing in ("mkfs.ext4", "sfdisk", "wipefs", "blkid"):
            self.tools.calls.clear()
            self.tools.missing = {missing}
            text = self.refused(self.request("sdd", table="gpt"))
            self.assertIn("not installed", text)
            self.assertEqual(self.tools.names(), [], missing)

    def test_a_failed_step_stops_the_sequence(self):
        self.tools.fail["sfdisk"] = "sfdisk: cannot open /dev/sdd: I/O error"
        text = self.refused(self.request("sdd", table="gpt"))
        self.assertIn("I/O error", text)
        self.assertNotIn("mkfs.ext4", self.tools.names())

    def test_the_result_is_read_back_not_assumed(self):
        """mkfs exited 0 but the device does not hold what was asked for."""
        self.tools.lie["sdd1"] = {"TYPE": "vfat"}
        text = self.refused(self.request("sdd", table="gpt", fstype="ext4"))
        self.assertIn("did not find", text)

    def test_a_label_with_a_space_reads_back_as_itself(self):
        """blkid's all-in-one output prints "My\\ disk"; compared with that,
        every name with a space in it looked as if it had been written
        wrongly."""
        result = self.helper.op_format(self.request(
            "sdd", table="gpt", fstype="exfat", label="My disk"))
        self.assertEqual(result["label"], "My disk")
        for args in self.tools.of("blkid"):
            self.assertIn("--match-tag", args)
            self.assertEqual(args[args.index("--output") + 1], "value")

    def test_two_signatures_at_once_are_not_guessed_between(self):
        def ambivalent(argv, stdin=None, timeout=None):
            if os.path.basename(argv[0]) == "blkid":
                return 8, "", "ambivalent result"
            return self.tools.run(argv, stdin, timeout)
        self.helper.run_tool = ambivalent
        text = self.refused(self.request("sdd", table="gpt"))
        self.assertIn("more than one filesystem", text)

    def test_a_label_that_reads_back_differently_is_reported(self):
        self.tools.lie["sdd1"] = {"TYPE": "ext4", "LABEL": "other"}
        text = self.refused(self.request("sdd", table="gpt", label="mine"))
        self.assertIn("reads back", text)

    def test_mbr_cannot_describe_a_drive_beyond_two_tebibytes(self):
        self.tree.add_disk("sde", 3000 * GIB, diskseq=9)
        text = self.refused(self.request("sde", table="dos"))
        self.assertIn("GPT", text)
        self.assertEqual(self.destructive(), [])

    def test_a_table_on_a_partition_is_refused(self):
        self.assertIn("whole drive",
                      self.refused(self.request("sdd1", table="gpt")))

    def test_a_whole_drive_without_a_table_is_refused(self):
        self.assertIn("partition table",
                      self.refused(self.request("sdd", table=None)))

    def test_too_small_for_the_filesystem(self):
        self.tree.add_disk("sde", 100 * MIB, diskseq=9)
        text = self.refused(self.request("sde", table="gpt", fstype="xfs"))
        self.assertIn("too small", text)
        self.assertEqual(self.destructive(), [])

    def test_the_steps_announced_match_the_plan_shown(self):
        """The confirmation lists steps; the helper must do those, in that
        order, or the window described something other than what ran."""
        cases = (("sdd", {"table": "gpt"}), ("sdb", {"table": "gpt"}),
                 ("sdd1", {}), ("sdb1", {}))
        inventory = {
            "sdd": disks.Disk("sdd", "/dev/sdd", 8 * GIB, removable=True,
                              transport="usb", volumes=(
                                  disks.Volume("sdd1", "/dev/sdd1", "sdd",
                                               "part", 8 * GIB - 2 * MIB),)),
            "sdb": disks.Disk("sdb", "/dev/sdb", 1000 * GIB, volumes=(
                disks.Volume("sdb1", "/dev/sdb1", "sdb", "part", 999 * GIB,
                             fstype="ext4", mountpoints=("/mnt/games",)),)),
        }
        for name, fields in cases:
            self.setUp()
            disk = inventory[name[:3]]
            volume = disk.volume(name)
            plan = diskops.plan_format(disk, volume, "ext4", "x",
                                       table=fields.get("table"),
                                       identity={})
            self.helper.op_format(self.request(name, label="x", **fields))
            keys = [e["key"] for e in self.events() if e.get("event") == "step"]
            self.assertEqual(keys, plan.step_keys, name)
            self.assertEqual([e["total"] for e in self.events()
                              if e.get("event") == "step"],
                             [len(plan.step_keys)] * len(keys))

    def test_a_signature_left_on_the_bare_drive_fails_the_check(self):
        """An installer image is a disc filesystem over the whole device.
        If that survived, the drive would go on showing the image."""
        original = self.tools.run

        def run(argv, stdin=None, timeout=None):
            result = original(argv, stdin, timeout)
            if os.path.basename(argv[0]) == "sfdisk":
                self.tools.contents["sdd"]["TYPE"] = "iso9660"
            return result

        self.helper.run_tool = run
        text = self.refused(self.request("sdd", table="gpt"))
        self.assertIn("iso9660", text)
        self.assertIn("/dev/sdd", text)

    def test_the_new_table_is_read_back(self):
        self.helper.op_format(self.request("sdd", table="dos"))
        self.assertEqual(self.tools.contents["sdd"], {"PTTYPE": "dos"})
        self.tools.lie["sdd"] = {"PTTYPE": "gpt"}
        text = self.refused(self.request("sdd", table="dos"))
        self.assertIn("reads back", text)


class FormatPartitionTest(HelperCase):
    def test_only_the_partition_is_touched(self):
        self.helper.op_format(self.request("sdd1", fstype="exfat",
                                           label="Stick"))
        self.assertEqual(self.squeezed(),
                         ["wipefs", "mkfs.exfat", "blkid", "udevadm", "blkid"])
        for name in ("wipefs", "mkfs.exfat"):
            self.assertEqual(os.path.basename(self.tools.of(name)[0][-1]),
                             "sdd1")

    def test_the_partition_type_follows_the_filesystem(self):
        self.tools.contents["sdd"] = {"PTTYPE": "gpt"}
        self.helper.op_format(self.request("sdd1", fstype="ntfs"))
        change = self.tools.of("sfdisk")[0]
        self.assertEqual(change[change.index("--part-type") + 1:],
                         [os.path.join(self.tree.dev, "sdd"), "1",
                          self.module.GPT_BASIC_DATA])

    def test_a_type_that_cannot_be_updated_is_a_warning_not_a_failure(self):
        self.tools.contents["sdd"] = {"PTTYPE": "dos"}
        self.tools.fail["sfdisk"] = "sfdisk: failed"
        result = self.helper.op_format(self.request("sdd1", fstype="ntfs"))
        self.assertIn("Windows may not show", result["warning"])

    def test_the_command_for_each_filesystem(self):
        expected = {
            "vfat": ("mkfs.fat", ["-F", "32", "-n", "LBL"]),
            "exfat": ("mkfs.exfat", ["-L", "lbl"]),
            "ntfs": ("mkfs.ntfs", ["--quick", "--label", "lbl"]),
            "ext4": ("mkfs.ext4", ["-F", "-L", "lbl"]),
            "btrfs": ("mkfs.btrfs", ["--force", "--label", "lbl"]),
            "xfs": ("mkfs.xfs", ["-f", "-L", "lbl"]),
            "f2fs": ("mkfs.f2fs", ["-f", "-l", "lbl"]),
        }
        self.assertEqual(set(expected), set(self.module.FILESYSTEMS))
        for fstype, (tool, arguments) in expected.items():
            self.tools.calls.clear()
            self.helper.op_format(self.request("sdd1", fstype=fstype,
                                               label="lbl"))
            argv = self.tools.of(tool)[0]
            self.assertEqual(argv[:-1], arguments, fstype)
            self.assertEqual(os.path.basename(argv[-1]), "sdd1")

    def test_no_tool_is_ever_told_to_format_a_mounted_device(self):
        """mkntfs --force and mke2fs -F -F are the switches that do."""
        for fstype in self.module.FILESYSTEMS:
            self.tools.calls.clear()
            self.helper.op_format(self.request("sdd1", fstype=fstype))
            for name, args, _stdin in self.tools.calls:
                if name.startswith("mkfs.ntfs"):
                    self.assertNotIn("--force", args)
                    self.assertNotIn("-F", args)
                if name == "mkfs.ext4":
                    self.assertEqual(args.count("-F"), 1)

    def test_a_small_volume_is_left_to_pick_its_own_fat_size(self):
        self.tree.add_disk("sde", 100 * MIB, diskseq=9)
        self.tree.add_partition("sde", "sde1", 1, 2048, 48 * MIB)
        self.helper.op_format(self.request("sde1", fstype="vfat"))
        self.assertNotIn("-F", self.tools.of("mkfs.fat")[0])

    def test_the_old_id_is_kept_when_asked(self):
        uuid = "0f3c2a61-5b7e-4d19-9a84-2c6e1b7d3f50"
        self.helper.op_format(self.request("sdd1", fstype="ext4", uuid=uuid))
        argv = self.tools.of("mkfs.ext4")[0]
        self.assertEqual(argv[argv.index("-U") + 1], uuid)

        self.tools.calls.clear()
        self.helper.op_format(self.request("sdd1", fstype="vfat",
                                           uuid="1A2B-3C4D"))
        argv = self.tools.of("mkfs.fat")[0]
        self.assertEqual(argv[argv.index("-i") + 1], "1A2B3C4D")

    def test_an_id_that_is_not_an_id_is_refused(self):
        for bad in ("; rm -rf /", "--help", "1234", 7):
            text = self.refused(self.request("sdd1", fstype="ext4", uuid=bad))
            self.assertIn("not valid", text)
        self.assertEqual(self.destructive(), [])

    def test_the_new_filesystem_belongs_to_whoever_asked(self):
        os.environ["PKEXEC_UID"] = str(os.getuid() or 1000)
        self.addCleanup(os.environ.pop, "PKEXEC_UID", None)
        if os.getuid() == 0:
            self.skipTest("needs an ordinary user to stand in for the caller")
        self.helper.op_format(self.request("sdd1", fstype="ext4", owner=True))
        argv = self.tools.of("mkfs.ext4")[0]
        owner = argv[argv.index("-E") + 1]
        self.assertEqual(owner, "root_owner=%d:%d" % (os.getuid(),
                                                      os.getgid()))

    def test_ownership_is_not_offered_to_a_filesystem_without_any(self):
        os.environ["PKEXEC_UID"] = "1000"
        self.addCleanup(os.environ.pop, "PKEXEC_UID", None)
        self.helper.op_format(self.request("sdd1", fstype="exfat", owner=True))
        self.assertNotIn("mount", self.tools.names())


class ClusterTest(HelperCase):
    """The cluster size asked for reaches mkfs, and is checked first and
    read back after."""

    def test_each_tool_is_told_the_size_its_own_way(self):
        for fstype, tool, flag, value in (
                ("exfat", "mkfs.exfat", "-c", "32768"),
                ("ntfs", "mkfs.ntfs", "--cluster-size", "65536"),
                # mkfs.fat counts in sectors: 4 KiB is 8 of 512 bytes.
                ("vfat", "mkfs.fat", "-s", "8")):
            self.tools.calls.clear()
            cluster = 65536 if fstype == "ntfs" else (
                32768 if fstype == "exfat" else 4096)
            self.helper.op_format(self.request("sdd1", fstype=fstype,
                                               cluster=cluster))
            argv = self.tools.of(tool)[0]
            self.assertEqual(argv[argv.index(flag) + 1], value, fstype)

    def test_fat_sectors_are_the_drives_own(self):
        self.tree.add_disk("sdf", 16 * GIB, diskseq=11, logical=4096)
        self.tree.add_partition("sdf", "sdf1", 1, 2048, 16 * GIB - 2 * MIB)
        self.helper.op_format(self.request("sdf1", fstype="vfat",
                                           cluster=8192))
        argv = self.tools.of("mkfs.fat")[0]
        self.assertEqual(argv[argv.index("-s") + 1], "2")

    def test_none_leaves_the_choice_to_the_tool(self):
        self.helper.op_format(self.request("sdd1", fstype="exfat",
                                           cluster=None))
        self.assertNotIn("-c", self.tools.of("mkfs.exfat")[0])

    def test_a_size_the_volume_cannot_have_is_refused_before_anything(self):
        for fstype, cluster in (("ext4", 4096), ("btrfs", 65536),
                                ("exfat", 3000), ("exfat", 64 * MIB),
                                ("exfat", 256), ("ntfs", 4 * MIB),
                                ("vfat", 128 * 1024), ("exfat", True),
                                ("exfat", "4096"), ("ntfs", -4096)):
            text = self.refused(self.request("sdd1", fstype=fstype,
                                             cluster=cluster))
            self.assertIn("cannot have clusters", text, (fstype, cluster))
        self.assertEqual(self.destructive(), [])

    def test_a_drive_with_large_sectors_has_no_smaller_clusters(self):
        self.tree.add_disk("sdf", 16 * GIB, diskseq=11, logical=4096)
        self.tree.add_partition("sdf", "sdf1", 1, 2048, 16 * GIB - 2 * MIB)
        text = self.refused(self.request("sdf1", fstype="exfat",
                                         cluster=2048))
        self.assertIn("cannot have clusters", text)

    def test_what_mkfs_made_is_read_back(self):
        self.tools.cluster_lie = 512 * 1024
        text = self.refused(self.request("sdd1", fstype="exfat",
                                         cluster=32768))
        self.assertIn("clusters of 524288 bytes instead of 32768", text)

    def test_boot_sectors_are_read_as_each_filesystem_writes_them(self):
        ntfs = bytearray(512)
        ntfs[0x0B:0x0D] = (512).to_bytes(2, "little")
        ntfs[0x0D] = 8
        self.assertEqual(self.module.boot_cluster("ntfs", ntfs), 4096)
        # 0x80 is still a count: 128 sectors, 64 KiB. Found by mkntfs on a
        # loop device - read as a power of two it was 2^128 sectors.
        ntfs[0x0D] = 0x80
        self.assertEqual(self.module.boot_cluster("ntfs", ntfs), 65536)
        # Above it the count is a negative power of two.
        ntfs[0x0D] = 0xF4
        self.assertEqual(self.module.boot_cluster("ntfs", ntfs), 2 * MIB)
        exfat = bytearray(512)
        exfat[108], exfat[109] = 9, 8
        self.assertEqual(self.module.boot_cluster("exfat", exfat), 128 * 1024)
        fat = bytearray(512)
        fat[11:13] = (4096).to_bytes(2, "little")
        fat[13] = 4
        self.assertEqual(self.module.boot_cluster("vfat", fat), 16384)
        self.assertEqual(self.module.boot_cluster("ext4", fat), 0)


class ClusterRulesTest(unittest.TestCase):
    """The helper's limits are the dialog's, so nothing offered is refused
    and nothing refused is offered."""

    def setUp(self):
        self.module = load_helper()

    def test_the_tables_are_the_same(self):
        for name in ("CLUSTER_RANGE", "FAT32_FROM", "FAT32_MIN_CLUSTERS",
                     "FAT32_MAX_CLUSTERS", "CLUSTER_MAX_COUNT",
                     "CLUSTER_MIN_COUNT"):
            self.assertEqual(getattr(self.module, name),
                             getattr(diskfs, name), name)

    def test_the_same_sizes_pass_both(self):
        sizes = (8 * MIB, 40 * MIB, 64 * MIB - 1, 64 * MIB, 100 * MIB,
                 257 * MIB, 600 * MIB, 4 * GIB, 8 * GIB, 33 * GIB,
                 500 * GIB, 2 * 1024 * GIB, 17 * 1024 * GIB,
                 300 * 1024 * GIB)
        clusters = [1 << n for n in range(8, 27)] + [3000, 0]
        for fstype in diskfs.FILESYSTEMS:
            for size in sizes:
                for sector in (512, 4096):
                    for cluster in clusters:
                        self.assertEqual(
                            self.module.cluster_ok(fstype, size, sector,
                                                   cluster),
                            diskfs.cluster_valid(fstype, size, sector,
                                                 cluster),
                            (fstype, size, sector, cluster))

    def test_everything_offered_is_accepted(self):
        for fstype in diskfs.FILESYSTEMS:
            for size in (100 * MIB, 8 * GIB, 2 * 1024 * GIB):
                choice = diskfs.cluster_choice(fstype, size, 512)
                for cluster in choice.sizes:
                    self.assertEqual(self.module.check_cluster(
                        fstype, size, 512, cluster), cluster)


class LabelRulesTest(unittest.TestCase):
    def setUp(self):
        self.module = load_helper()

    def test_limits_match_what_the_dialog_enforces(self):
        """Two copies of the same table, because the helper cannot import
        the application; this is what keeps them the same."""
        self.assertEqual(set(self.module.FILESYSTEMS), set(diskfs.FILESYSTEMS))
        for key, fs in diskfs.FILESYSTEMS.items():
            spec = self.module.FILESYSTEMS[key]
            self.assertEqual(spec["label"], (fs.label_max, fs.label_counts),
                             key)
            self.assertEqual(spec["min"], fs.min_size, key)
            self.assertEqual(spec["max"], fs.max_size, key)
            self.assertEqual(spec["native"], fs.native, key)
            self.assertEqual(spec["mkfs"], fs.mkfs, key)
            self.assertEqual(not spec.get("no_spaces"), fs.label_spaces, key)
        self.assertEqual(set(self.module.FAT_FORBIDDEN),
                         set(diskfs.FAT_FORBIDDEN))

    def test_a_label_the_dialog_accepts_the_helper_accepts(self):
        samples = ["", "USB", "My Disk", "Fotos 2026", "ÄÖÜ", "日本語",
                   "a" * 11, "a" * 12, "a" * 16, "a" * 17, "a" * 32,
                   "a" * 33, "x.y", "a/b", "-dash", "tab\there"]
        for key, fs in diskfs.FILESYSTEMS.items():
            for sample in samples:
                label, problem = diskfs.validate_label(fs, sample)
                try:
                    checked = self.module.check_label(key, sample)
                except self.module.Refusal:
                    self.assertIsNotNone(problem, (key, sample))
                else:
                    self.assertIsNone(problem, (key, sample))
                    self.assertEqual(checked, label, (key, sample))

    def test_fat_labels_are_stored_in_capitals(self):
        self.assertEqual(self.module.check_label("vfat", "photos"), "PHOTOS")

    def test_a_label_cannot_pose_as_an_option(self):
        for key in self.module.FILESYSTEMS:
            with self.assertRaises(self.module.Refusal):
                self.module.check_label(key, "--force")


class ToolOutputTest(HelperCase):
    """What a tool prints reaches the client, for the details view."""

    def test_a_counter_redrawn_with_backspaces_is_followed(self):
        """mke2fs redraws "Writing inode tables: N/M" with backspaces and
        ends the line only when the phase is done."""
        script = ("printf 'Writing inode tables:  1/16'; sleep 0.4; "
                  "printf '\\b\\b\\b\\b\\b  9/16'; sleep 0.4; "
                  "printf '\\b\\b\\b\\b\\bdone \\nCreating journal\\n'")
        lines, partials = [], []
        code, stopped = self.module._stream_tool(
            ["/bin/sh", "-c", script], lines.append, lambda: False,
            timeout=30, on_partial=partials.append)
        self.assertEqual((code, stopped), (0, False))
        self.assertTrue(partials[0].endswith("1/16"), partials)
        self.assertTrue(partials[-1].endswith("9/16"), partials)
        self.assertEqual([line.split() for line in lines],
                         [["Writing", "inode", "tables:", "done"],
                          ["Creating", "journal"]])

    def test_rendering_overwrites_like_a_terminal(self):
        render = self.module._render
        self.assertEqual(render(b"ab\bc"), b"ac")
        self.assertEqual(render(b"\b\bx"), b"x")
        self.assertEqual(render(b"plain"), b"plain")

    def test_mkfs_output_and_counters_reach_the_client(self):
        self.tools.lines["mkfs.ext4"] = [
            "Creating filesystem with 2096896 4k blocks",
            ("partial", "Writing inode tables:  12/64"),
            "Writing inode tables: done",
            "Creating journal (16384 blocks): done"]
        self.helper.OUTPUT_INTERVAL = 0
        self.helper.op_format(self.request("sdd1", fstype="ext4"))
        events = self.events()
        said = [e["text"] for e in events if e.get("event") == "output"]
        self.assertIn("Writing inode tables:  12/64", said)
        self.assertEqual(said[-1], "Creating journal (16384 blocks): done")
        counted = [e for e in events if e.get("event") == "progress"]
        self.assertEqual([(e["done"], e["total"], e["unit"])
                          for e in counted], [(12, 64, "items")])
        # Each line goes out under the request it belongs to.
        self.assertEqual({e["id"] for e in events
                          if e.get("event") == "output"}, {1})

    def test_lines_that_come_too_fast_leave_the_last_one_sent(self):
        self.tools.lines["mkfs.ext4"] = ["one", "two", "three",
                                         "Writing superblocks"]
        self.helper.OUTPUT_INTERVAL = 3600
        self.helper.op_format(self.request("sdd1", fstype="ext4"))
        said = [e["text"] for e in self.events()
                if e.get("event") == "output"]
        self.assertEqual(said[0], "one")
        self.assertEqual(said[-1], "Writing superblocks")
        self.assertNotIn("two", said)

    def test_a_failing_mkfs_still_says_why(self):
        self.tools.fail["mkfs.ext4"] = "mke2fs: Device size reported to be zero."
        text = self.refused(self.request("sdd1", fstype="ext4"))
        self.assertIn("Creating the filesystem failed", text)
        self.assertIn("Device size reported to be zero", text)


class OverwriteTest(HelperCase):
    def device_bytes(self, name):
        with open(os.path.join(self.tree.dev, name), "rb") as handle:
            return handle.read()

    def test_a_full_format_zeroes_the_whole_device(self):
        self.tree.add_disk("sde", 16 * MIB, diskseq=9)
        self.tree.add_partition("sde", "sde1", 1, 2048, 9 * MIB)
        with open(os.path.join(self.tree.dev, "sde1"), "wb") as handle:
            handle.write(b"\xa5" * 9 * MIB)
        # An odd chunk size, so the last write is a partial one.
        self.module.CHUNK = 4 * MIB

        self.helper.op_format(self.request("sde1", erase="zero"))
        self.assertEqual(self.device_bytes("sde1"), b"\x00" * 9 * MIB)
        progress = [e for e in self.events() if e.get("event") == "progress"]
        self.assertEqual(progress[-1]["done"], progress[-1]["total"])
        self.assertEqual(progress[-1]["total"], 9 * MIB)
        # Zeroing does not stand in for removing signatures or for mkfs.
        self.assertEqual(self.tools.names()[:2], ["wipefs", "mkfs.ext4"])

    def test_signatures_go_before_anything_else_is_erased(self):
        """A GPT keeps a second copy at the end of the disk. An overwrite
        stopped halfway, or a discard that does not read back as zeros,
        would leave it describing partitions that are gone."""
        self.helper.op_format(self.request("sdd1", erase="discard"))
        self.assertEqual(self.tools.names()[:3],
                         ["wipefs", "blkdiscard", "mkfs.ext4"])

        self.tools.calls.clear()
        wiped_first = []
        original = self.helper.overwrite
        self.helper.overwrite = lambda request, device: (
            wiped_first.append(list(self.tools.names())),
            original(request, device))
        self.tree.add_disk("sde", 8 * MIB, diskseq=9)
        self.tree.add_partition("sde", "sde1", 1, 2048, 4 * MIB)
        self.module.FILESYSTEMS["ext4"]["min"] = MIB
        self.helper.op_format(self.request("sde", table="gpt", erase="zero"))
        self.assertEqual(wiped_first, [["wipefs", "wipefs"]])

    def test_no_tool_is_told_to_ignore_that_a_device_is_in_use(self):
        """--force is how wipefs and blkdiscard are told to skip their own
        exclusive open, which is their check that nothing else has it."""
        for erase in ("none", "discard"):
            self.tools.calls.clear()
            self.helper.op_format(self.request("sdb", table="gpt",
                                               erase=erase))
            for name in ("wipefs", "blkdiscard"):
                for args in self.tools.of(name):
                    self.assertNotIn("--force", args, name)
                    self.assertNotIn("-f", args, name)

    def test_stopping_during_the_overwrite_says_what_was_lost(self):
        self.tree.add_disk("sde", 32 * MIB, diskseq=9)
        self.tree.add_partition("sde", "sde1", 1, 2048, 30 * MIB)
        self.module.CHUNK = MIB
        original = self.helper.progress

        def cancel_soon(request, done, total):
            original(request, done, total)
            self.helper.cancel.set()
        self.helper.progress = cancel_soon
        self.module.PROGRESS_INTERVAL = 0

        self.helper.run_job(self.request("sde1", erase="zero"))
        reply = self.events()[-1]
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["cancelled"])
        self.assertIn("no usable filesystem", reply["error"])
        self.assertNotIn("mkfs.ext4", self.tools.names())

    def test_a_stop_is_ignored_once_the_table_is_being_written(self):
        """Half a partition table helps nobody: short steps run to the end."""
        self.helper.cancel.set()
        self.helper.run_job(self.request("sdd", table="gpt"))
        reply = self.events()[-1]
        self.assertTrue(reply["ok"], reply)
        self.assertIn("mkfs.ext4", self.tools.names())


class LabelOperationTest(HelperCase):
    def label(self, name, text):
        return {"op": "label", "id": 4, "label": text,
                "device": os.path.join(self.tree.dev, name),
                "identity": self.tree.identity(name)}

    def test_renaming_a_mounted_volume_unmounts_it_first(self):
        result = self.helper.op_label(self.label("sdb1", "Steam"))
        self.assertEqual(self.squeezed(),
                         ["blkid", "umount", "e2label", "udevadm", "blkid"])
        self.assertEqual(self.tools.of("e2label")[0][1], "Steam")
        self.assertTrue(result["was_mounted"])

    def test_a_system_volume_is_not_renamed(self):
        with self.assertRaises(self.module.Refusal) as caught:
            self.helper.op_label(self.label("nvme0n1p2", "x"))
        self.assertIn("running system", str(caught.exception))
        self.assertEqual(self.tools.names(), [])

    def test_an_unformatted_partition_has_no_label_to_change(self):
        with self.assertRaises(self.module.Refusal) as caught:
            self.helper.op_label(self.label("sdd1", "x"))
        self.assertIn("cannot be changed", str(caught.exception))

    def test_the_label_is_checked_for_the_filesystem_it_is_on(self):
        with self.assertRaises(self.module.Refusal):
            self.helper.op_label(self.label("sdb1", "a" * 17))
        self.assertNotIn("e2label", self.tools.names())

    def test_every_renamable_filesystem_has_a_tool_on_both_sides(self):
        self.assertEqual(set(self.module.LABEL_TOOLS),
                         set(diskops.LABEL_TOOLS))
        for key, (tool, _arguments) in self.module.LABEL_TOOLS.items():
            self.assertEqual(tool, diskops.LABEL_TOOLS[key])


class LockTest(HelperCase):
    def test_a_second_operation_waits_its_turn(self):
        with self.helper.locked():
            text = self.refused(self.request("sdd1"))
        self.assertIn("already running", text)
        self.assertEqual(self.destructive(), [])
        self.helper.op_format(self.request("sdd1"))


class ProtocolTest(HelperCase):
    def say(self, message):
        self.out.seek(0)
        self.out.truncate()
        self.helper.handle(json.dumps(message))
        if self.helper.job is not None:
            self.helper.job.join()
        return self.events()

    def test_hello_refuses_another_version(self):
        reply = self.say({"cmd": "hello", "id": 1, "version": 999})[-1]
        self.assertFalse(reply["ok"])
        self.assertIn("protocol version", reply["error"])
        reply = self.say({"cmd": "hello", "id": 2,
                          "version": self.module.PROTOCOL_VERSION})[-1]
        self.assertTrue(reply["ok"])

    def test_the_client_and_the_helper_agree_on_the_version(self):
        from ksyssupervisor import diskctl
        self.assertEqual(self.module.PROTOCOL_VERSION,
                         diskctl.PROTOCOL_VERSION)

    def test_garbage_is_answered_not_fatal(self):
        self.helper.handle("{not json")
        self.helper.handle("[1, 2]")
        self.assertEqual([e["ok"] for e in self.events()], [False, False])

    def test_an_unknown_operation_is_refused(self):
        reply = self.say({"cmd": "run", "id": 3, "op": "shred-everything"})[-1]
        self.assertFalse(reply["ok"])

    def test_a_refusal_comes_back_as_the_reply(self):
        reply = self.say(dict(self.request("nvme0n1p2"), cmd="run", id=5))[-1]
        self.assertEqual(reply["id"], 5)
        self.assertFalse(reply["ok"])
        self.assertIn("running system", reply["error"])

    def test_a_crash_inside_an_operation_is_still_an_answer(self):
        def explode(*_args):
            raise RuntimeError("boom")
        self.helper.unmount = explode
        self.helper.mounts_of = lambda device: ["/mnt/x"]
        reply = self.say(dict(self.request("sdd1"), cmd="run", id=6))[-1]
        self.assertFalse(reply["ok"])
        self.assertIn("boom", reply["error"])

    def test_only_one_operation_at_a_time(self):
        gate = threading.Event()
        self.helper.OPERATIONS = dict(self.helper.OPERATIONS,
                                      wait=lambda helper, request: gate.wait(5))
        self.helper.handle(json.dumps({"cmd": "run", "id": 1, "op": "wait"}))
        self.helper.handle(json.dumps({"cmd": "run", "id": 2, "op": "wait"}))
        busy = [e for e in self.events() if e.get("id") == 2][0]
        self.assertIn("still running", busy["error"])
        gate.set()
        self.helper.job.join()

    def test_tools_never_read_the_protocol_pipe(self):
        """A tool that stops to ask a question must find nothing to read."""
        code, out, _err = self.module._run_tool(["/usr/bin/cat"], None, 10)
        self.assertEqual((code, out), (0, ""))


class HelperProcessTest(unittest.TestCase):
    """The real script as a real process, talking over real pipes."""

    BOOTSTRAP = """
import importlib.machinery, importlib.util, os, sys
sys.path.insert(0, %(tests)r)
from disktree import FakeBlockTree, load_helper
module = load_helper()
tree = FakeBlockTree(%(root)r)
tree.add_disk("nvme0n1", 500 * 1024 ** 3)
tree.add_partition("nvme0n1", "nvme0n1p2", 2, 2048, 400 * 1024 ** 3)
tree.mount("nvme0n1p2", "/")
tree.install_helper(module)
sys.exit(module.main())
"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        script = self.BOOTSTRAP % {
            "tests": os.path.dirname(os.path.abspath(__file__)),
            "root": os.path.join(self.root, "tree")}
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop)

    def _stop(self):
        import shutil
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        self.proc.wait(timeout=10)
        shutil.rmtree(self.root, ignore_errors=True)

    def ask(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def test_it_greets_refuses_the_system_disk_and_leaves(self):
        module = load_helper()
        self.assertTrue(self.ask({"cmd": "hello", "id": 1,
                                  "version": module.PROTOCOL_VERSION})["ok"])
        reply = self.ask({
            "cmd": "run", "id": 2, "op": "format", "fstype": "ext4",
            "device": os.path.join(self.root, "tree", "dev", "nvme0n1p2"),
            "identity": {"name": "nvme0n1p2", "size": 400 * 1024 ** 3,
                         "diskseq": 1, "start": 2048}})
        self.assertFalse(reply["ok"])
        self.assertIn("running system", reply["error"])
        self.assertTrue(self.ask({"cmd": "bye", "id": 3})["ok"])
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def test_it_exits_when_the_client_disappears(self):
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=10), 0)

    def test_the_script_is_standalone(self):
        """It runs as root outside the application's environment."""
        with open(HELPER) as handle:
            source = handle.read()
        self.assertNotIn("ksyssupervisor import", source)
        self.assertNotIn("import ksyssupervisor", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)


if __name__ == "__main__":
    unittest.main()
