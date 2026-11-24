"""Looking after a volume: TRIM, fragmentation, free space, checking.

What each of those means for the drive and filesystem in hand, in the words
shown to the user, and the requests that have the privileged helper do them.
The helper decides for itself whether it will; nothing here is the safety
mechanism.

Qt-free.
"""

from __future__ import annotations

import os
import subprocess

from . import diskfs, diskops, disks
from .diskfs import INFO, WARNING, Advice
from .diskops import Plan, describe_target

MOUNTINFO = "/proc/self/mountinfo"

#: The directory free space is filled through; the helper's name for it.
WIPE_PREFIX = ".ksyssupervisor-wipe-"

PATTERNS = (
    ("zero", "Zeros", "Every free block is filled with zeros. The usual "
                      "choice."),
    ("one", "Ones", "Every free block is filled with ones."),
    ("random", "Random data", "Every free block is filled with random data. "
                              "Slower to produce, and what a compressing "
                              "filesystem needs."),
)


# ---- reading the system --------------------------------------------------------

def mount_options(mountpoint, path=None):
    """The options a mount point was mounted with, as a set; empty if it is
    not a mount point."""
    try:
        with open(path or MOUNTINFO) as handle:
            lines = handle.read().splitlines()
    except OSError:
        return set()
    wanted = mountpoint.replace(" ", "\\040")
    found = set()
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 7 or fields[4] != wanted:
            continue
        found = set(fields[5].split(","))
        try:
            dash = fields.index("-")
            found |= set(fields[dash + 3].split(","))
        except (ValueError, IndexError):
            pass
    return found


def _systemctl(*args):
    done = subprocess.run(("systemctl",) + args, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=15,
                          env=dict(os.environ, LC_ALL="C"))
    return done.stdout.decode("utf-8", "replace").strip()


def timer_enabled(run=None):
    """Whether the system trims every mounted drive weekly: True, False, or
    None where that cannot be told (no systemd, no such timer)."""
    try:
        state = (run or _systemctl)("is-enabled", "fstrim.timer")
    except (OSError, subprocess.SubprocessError):
        return None
    if state in ("enabled", "enabled-runtime"):
        return True
    if state in ("disabled", "masked"):
        return False
    return None


def leftovers(volume, listdir=os.listdir):
    """Fill directories an interrupted free-space wipe left on a volume."""
    found = []
    for point in volume.all_mountpoints():
        if point == disks.SWAP:
            continue
        try:
            names = listdir(point)
        except OSError:
            continue
        found.extend(os.path.join(point, name) for name in names
                     if name.startswith(WIPE_PREFIX))
    return found


# ---- TRIM ---------------------------------------------------------------------

def trim_state(disk, volume, timer, options=None):
    """How this volume's free blocks get reported to the drive, in a line.

    `timer`: timer_enabled(). `options`: the mount options, read here when
    not given.
    """
    if not disk.discard:
        return ("Not available: this drive, or the adapter it is connected "
                "through, does not accept TRIM.")
    if volume is None or not volume.mounted:
        return "The drive accepts TRIM. A volume is trimmed while mounted."
    if options is None:
        options = mount_options(volume.mountpoint) if volume.mountpoint \
            else set()
    continuous = any(o == "discard" or o.startswith("discard=")
                     for o in options)
    if continuous and timer:
        return "Continuous (a mount option) and weekly (the system's timer)."
    if continuous:
        return "Continuous: the filesystem reports blocks as they are freed."
    if timer:
        return "Weekly, by the system's timer."
    if timer is None:
        return "Not automatic on this volume."
    return ("Not automatic: the weekly TRIM is switched off and the volume "
            "is not mounted with 'discard'.")


def trim_done_text(result):
    amount = result.get("trimmed")
    where = result.get("mountpoint") or result.get("device", "")
    if amount is None:
        return "TRIM of %s finished." % where
    if amount == 0:
        return ("TRIM of %s finished: nothing new to report, the drive "
                "already knew." % where)
    return ("TRIM of %s finished: the drive was told that %s is unused."
            % (where, disks.format_size(amount)))


def plan_trim(disk, volume, identity=None):
    return Plan(
        title="TRIM", target=describe_target(disk, volume),
        steps=["Tell the drive which blocks of this volume are unused"],
        step_keys=["trim"],
        request={"op": "trim", "device": volume.path,
                 "identity": identity if identity is not None
                 else disks.identity(volume.name)})


def plan_timer(enable):
    return Plan(
        title="Weekly TRIM", target="all drives",
        steps=["Switch the system's weekly TRIM %s"
               % ("on" if enable else "off")],
        step_keys=["timer"],
        request={"op": "trim_timer", "enable": bool(enable)})


# ---- fragmentation --------------------------------------------------------------

SSD_DEFRAG_WARNING = Advice(
    WARNING, "Do not defragment a solid-state drive",
    "Defragmenting rewrites large parts of the drive. On an SSD, a memory "
    "stick or a card that is wear with nothing gained: there is no moving "
    "head, so a fragmented file reads exactly as fast as a tidy one. It can "
    "take a noticeable part of the drive's life. What an SSD benefits from "
    "is TRIM, which is on the same menu.")

BTRFS_DEFRAG_WARNING = Advice(
    WARNING, "Defragmenting Btrfs can use much more space",
    "Files that share their data - snapshots, and copies made instantly - "
    "stop sharing it when they are defragmented, so each takes its full "
    "size afterwards. On a volume with snapshots this can fill the drive.")


def fragmentation_advice(result, disk):
    """What a measurement means, as one Advice.

    The verdict comes from the tool that made the measurement and the bands
    it publishes; this only puts it into words.
    """
    verdict = result.get("verdict")
    extents = result.get("extents") or [0, 0]
    if verdict == "unrated":
        return Advice(
            INFO, "XFS rarely needs defragmenting",
            "XFS keeps files in order by itself. Its own tool reports %d "
            "pieces where %d would be ideal (%.1f%%) and describes that "
            "figure as largely meaningless, so no verdict is drawn from it."
            % (extents[0], extents[1], result.get("factor", 0.0)))
    score = result.get("score", 0)
    low, high = result.get("low", 30), result.get("high", 55)
    scale = ("Score %d on the tool's scale: up to %d is no problem, up to "
             "%d a little fragmented, above that worth defragmenting."
             % (score, low, high))
    if verdict == "none":
        return Advice(
            INFO, "Defragmenting is not needed",
            "%s It would add hours of work on the drive for no gain you "
            "would notice." % scale)
    if verdict == "some":
        return Advice(
            INFO, "A little fragmented - defragmenting is optional",
            "%s The gain would be small. Leave it unless large files on "
            "this drive feel slow to open." % scale)
    return Advice(
        WARNING if disk.is_solid_state else INFO,
        "Fragmented - defragmenting is worth it"
        if not disk.is_solid_state else
        "Fragmented, but this is a solid-state drive",
        scale)


def defrag_warnings(disk, volume):
    """The warnings that go in front of a defragmentation, worst first."""
    found = []
    if disk.is_solid_state:
        found.append(SSD_DEFRAG_WARNING)
    if volume is not None and volume.fstype == "btrfs":
        found.append(BTRFS_DEFRAG_WARNING)
    return found


def plan_analyse(disk, volume, identity=None):
    return Plan(
        title="Fragmentation", target=describe_target(disk, volume),
        steps=["Measure how fragmented the files are. Nothing is changed."],
        step_keys=["analyse"],
        request={"op": "defrag_analyse", "device": volume.path,
                 "identity": identity if identity is not None
                 else disks.identity(volume.name)})


def plan_defrag(disk, volume, identity=None):
    return Plan(
        title="Defragment", target=describe_target(disk, volume),
        steps=["Rewrite fragmented files in one piece. The volume stays in "
               "use meanwhile; files that are open are skipped."],
        step_keys=["defrag"],
        advice=defrag_warnings(disk, volume),
        request={"op": "defrag", "device": volume.path,
                 "identity": identity if identity is not None
                 else disks.identity(volume.name)})


def defrag_done_text(result):
    files, skipped = result.get("files"), result.get("skipped") or 0
    if files is None:
        return "Defragmenting finished."
    text = "Defragmenting finished: %d file%s done" % (
        files, "" if files == 1 else "s")
    if skipped:
        text += (", %d skipped (in use, or of a kind that cannot be moved)"
                 % skipped)
    return text + "."


# ---- free space -----------------------------------------------------------------

def wipe_advice(disk, volume, options=None):
    """What to know before overwriting a volume's free space."""
    found = []
    if options is None:
        options = mount_options(volume.mountpoint) if volume.mountpoint \
            else set()
    if disk.is_solid_state:
        found.append(Advice(
            WARNING, "On a solid-state drive, TRIM does this better",
            "Overwriting free space writes the whole empty part of the "
            "drive once, which is wear, and a solid-state drive may still "
            "keep old data in blocks it has set aside. TRIM tells the drive "
            "to drop the unused blocks itself, in seconds and without the "
            "wear. Use this only if TRIM is not available."
            if disk.discard else
            "Overwriting free space writes the whole empty part of the "
            "drive once, which is wear, and a solid-state drive may still "
            "keep old data in blocks it has set aside. This drive does not "
            "accept TRIM, so overwriting is what there is."))
    compressing = any(o.startswith("compress") for o in options)
    if volume.fstype == "btrfs" or compressing:
        found.append(Advice(
            INFO, "Random data will be used",
            "This filesystem compresses what is written to it, and zeros "
            "or ones compress to nothing: the free blocks would never be "
            "reached. Random data is written whatever is chosen above."))
    if volume.is_system:
        found.append(Advice(
            INFO, "A little room is left free",
            "The running system uses this volume, so a small reserve is "
            "kept free while this runs and that part is not overwritten. "
            "The computer may feel slow until it finishes."))
    found.append(Advice(
        INFO, "What this does not reach",
        "Files are not touched. Names of deleted files, and the remains "
        "of very small ones, can survive in the filesystem's own records; "
        "only a full format of the volume removes those."))
    return found


def plan_wipe(disk, volume, pattern, identity=None):
    if pattern not in [key for key, _title, _text in PATTERNS]:
        raise ValueError("Unknown pattern.")
    free = volume.free
    amount = (" (%s)" % disks.format_size(free)) if free else ""
    return Plan(
        title="Wipe free space", target=describe_target(disk, volume),
        steps=["Fill the free space%s with new files of %s" % (
                   amount, {"zero": "zeros", "one": "ones",
                            "random": "random data"}[pattern]),
               "Delete those files again"],
        step_keys=["fill", "remove"],
        advice=wipe_advice(disk, volume),
        request={"op": "wipe_free", "device": volume.path,
                 "pattern": pattern,
                 "identity": identity if identity is not None
                 else disks.identity(volume.name)})


def plan_wipe_cleanup(disk, volume, identity=None):
    return Plan(
        title="Remove leftover files", target=describe_target(disk, volume),
        steps=["Delete the files an interrupted free-space wipe left behind"],
        step_keys=[],
        request={"op": "wipe_cleanup", "device": volume.path,
                 "identity": identity if identity is not None
                 else disks.identity(volume.name)})


def wipe_done_text(result):
    text = "Free space overwritten: %s written" % disks.format_size(
        result.get("written", 0))
    if result.get("forced"):
        text += " as random data, because this filesystem compresses"
    if result.get("reserve"):
        text += "; %s was left free on purpose" % disks.format_size(
            result["reserve"])
    return text + "."


# ---- checking -------------------------------------------------------------------

def plan_check(disk, volume, repair=False, identity=None):
    steps, keys = [], []
    if volume.mounted:
        steps.append("Unmount %s" % (volume.mountpoint or volume.name))
        keys.append("unmount")
    steps.append("Check the filesystem. Nothing is changed.")
    keys.append("check")
    advice = []
    if repair:
        steps += ["Repair what the check found", "Check again"]
        keys += ["repair", "verify"]
        advice.append(Advice(
            WARNING, "A repair changes the filesystem",
            "It usually puts things right, but where the damage is bad it "
            "can remove files it cannot make sense of. If what is on this "
            "volume matters and is not copied elsewhere, copy it first - "
            "above all if the drive's health is not Good."))
    return Plan(
        title="Repair" if repair else "Check for errors",
        target=describe_target(disk, volume), steps=steps, step_keys=keys,
        advice=advice, remount=volume.mounted,
        request={"op": "check", "device": volume.path,
                 "repair": bool(repair),
                 "identity": identity if identity is not None
                 else disks.identity(volume.name)})


CHECK_TITLES = {
    "clean": "No errors found",
    "errors": "Errors found",
    "repaired": "Repaired",
    "unrepaired": "Errors remain after the repair",
}


def check_summary(result, volume=None):
    """(title, text, can a repair be offered) for a check's result."""
    state = result.get("state")
    fstype = result.get("fstype", "")
    title = CHECK_TITLES.get(state, "Checked")
    if state == "clean":
        text = "The filesystem is consistent."
    elif state == "errors":
        text = ("The filesystem has errors. Nothing has been changed yet. "
                "Files on it may be damaged or unreachable until it is "
                "repaired.")
    elif state == "repaired":
        text = ("The errors were repaired, and a second check found the "
                "filesystem consistent. Files the repair could not place "
                "are in a 'lost+found' or 'FOUND.000' folder at the top of "
                "the volume, if there are any.")
    else:
        text = ("The repair ran, but a second check still finds errors. "
                "Copy what you can off this volume. If the drive's health "
                "is not Good, the drive itself is the likely cause.")
    if result.get("note"):
        text += "\n\n" + result["note"]
    repairable = state == "errors" and fstype != "btrfs"
    if state == "errors" and fstype == "btrfs":
        text += ("\n\nBtrfs's own repair tool is not offered: its "
                 "documentation warns that it can make things worse. Copy "
                 "what you need off the volume; 'btrfs scrub' fixes what it "
                 "can while the volume is mounted.")
    return title, text, repairable


# ---- availability ---------------------------------------------------------------

#: Filesystems the helper can check, and the tool each needs.
CHECK_TOOLS = {
    "ext2": "e2fsck", "ext3": "e2fsck", "ext4": "e2fsck",
    "vfat": "fsck.fat", "exfat": "fsck.exfat", "ntfs": "ntfsfix",
    "xfs": "xfs_repair", "btrfs": "btrfs", "f2fs": "fsck.f2fs",
}
DEFRAG_TOOLS = {"ext4": "e4defrag", "xfs": "xfs_fsr", "btrfs": "btrfs"}
ANALYSE_TOOLS = {"ext4": "e4defrag", "xfs": "xfs_db"}

#: (Fedora, Debian/Ubuntu, Arch) package of each tool.
PACKAGES = {
    "e2fsck": ("e2fsprogs",) * 3, "e4defrag": ("e2fsprogs",) * 3,
    "fsck.fat": ("dosfstools",) * 3, "fsck.exfat": ("exfatprogs",) * 3,
    "ntfsfix": ("ntfsprogs", "ntfs-3g", "ntfs-3g"),
    "xfs_repair": ("xfsprogs",) * 3, "xfs_fsr": ("xfsprogs",) * 3,
    "xfs_db": ("xfsprogs",) * 3, "btrfs": ("btrfs-progs",) * 3,
    "fsck.f2fs": ("f2fs-tools",) * 3, "fstrim": ("util-linux",) * 3,
}

WINDOWS_FILESYSTEMS = ("ntfs", "vfat", "exfat")


def missing(tool):
    fedora, debian, arch = PACKAGES[tool]
    return diskops.no(
        "The '%s' command is not installed.\n"
        "Fedora: sudo dnf install %s\n"
        "Debian/Ubuntu: sudo apt install %s\n"
        "Arch: sudo pacman -S %s" % (tool, fedora, debian, arch))


def _needs_volume(volume):
    if volume is None:
        return diskops.no("Select a volume for this.")
    if volume.is_extended or not volume.fstype:
        return diskops.no("There is no filesystem here.")
    state = diskfs.windows_state(volume)
    if state is not None:
        return diskops.no(state.text)
    if volume.is_encrypted or volume.inner:
        return diskops.no("This is an encrypted or layered volume. Select "
                          "the volume inside it.")
    if volume.fstype in diskops.NOT_MOUNTABLE:
        return diskops.no("%s is not a filesystem."
                          % diskfs.title_of(volume.fstype))
    return None


def _needs_mounted(volume, why):
    if not volume.mounted:
        return diskops.no("Mount the volume first: %s" % why)
    return None


def availability(action, disk, volume=None, tools=diskfs.find_tool):
    """Whether a maintenance action can be done here, and if not, why."""
    if not disk.has_media:
        return diskops.no("There is no media in this device.")
    problem = _needs_volume(volume)
    if problem is not None:
        return problem

    if action == diskops.CHECK:
        if volume.is_system:
            return diskops.no(
                "This volume holds the running system (%s). A filesystem "
                "cannot be checked while it is in use; the system checks "
                "its own volumes when it starts."
                % ", ".join(volume.system_mounts))
        tool = CHECK_TOOLS.get(volume.fstype)
        if tool is None:
            return diskops.no("A %s volume cannot be checked from here."
                              % diskfs.title_of(volume.fstype))
        if tools(tool) is None:
            return missing(tool)
        return diskops.YES

    if action == diskops.TRIM:
        if not disk.discard:
            return diskops.no(
                "This drive, or the adapter it is connected through, does "
                "not accept TRIM. Hard disks have no use for it, and many "
                "USB enclosures do not pass it on.")
        problem = _needs_mounted(volume, "TRIM works through the mounted "
                                         "filesystem.")
        if problem is not None:
            return problem
        if tools("fstrim") is None:
            return missing("fstrim")
        return diskops.YES

    if action in (diskops.ANALYSE, diskops.DEFRAG):
        table = ANALYSE_TOOLS if action == diskops.ANALYSE else DEFRAG_TOOLS
        if volume.fstype in WINDOWS_FILESYSTEMS:
            return diskops.no(
                "%s cannot be defragmented from Linux: no tool for it "
                "exists. Windows defragments its own drives automatically; "
                "do it from there if it is ever needed."
                % diskfs.title_of(volume.fstype))
        if action == diskops.ANALYSE and volume.fstype == "btrfs":
            return diskops.no(
                "Btrfs has no tool that measures fragmentation.")
        tool = table.get(volume.fstype)
        if tool is None:
            return diskops.no("A %s volume cannot be defragmented."
                              % diskfs.title_of(volume.fstype))
        if volume.fstype != "xfs" or action == diskops.DEFRAG:
            problem = _needs_mounted(volume, "this works on the mounted "
                                             "filesystem, while it is in use.")
            if problem is not None:
                return problem
        if tools(tool) is None:
            return missing(tool)
        return diskops.YES

    if action == diskops.WIPE_FREE:
        if volume.fstype in ("iso9660", "udf", "squashfs", "erofs"):
            return diskops.no("%s is a read-only filesystem: it has no "
                              "free space to write to."
                              % diskfs.title_of(volume.fstype))
        if disk.read_only or volume.read_only:
            return diskops.no("This volume is read-only.")
        problem = _needs_mounted(volume, "free space is overwritten by "
                                         "writing files into it.")
        if problem is not None:
            return problem
        return diskops.YES

    return diskops.no("Not available.")


def optimise(disk, volume, tools=diskfs.find_tool):
    """What "Optimise" should do here: the action, or None with the reason.

    The same rule Windows applies: TRIM what takes TRIM, look at
    fragmentation on what spins.
    """
    if disk.is_solid_state or disk.discard:
        answer = availability(diskops.TRIM, disk, volume, tools)
        return (diskops.TRIM if answer else None), answer
    answer = availability(diskops.ANALYSE, disk, volume, tools)
    return (diskops.ANALYSE if answer else None), answer
