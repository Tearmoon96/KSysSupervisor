"""Editing the partition table: new table, create, delete, resize, move.

What can be done to which partition, what the user should know first, and
the requests that have the privileged helper do it. The helper works out
every limit again for itself, from the disk and with the filesystems' own
tools; what is computed here is for greying out and explaining.

Qt-free.
"""

from __future__ import annotations

import json
import os

from . import diskfs, diskops, disks
from .diskfs import INFO, WARNING, Advice
from .diskops import Plan, describe_target, what_is_lost

MIB = disks.MIB

#: Where the helper records a partition move while it is under way.
MOVES_DIR = "/var/lib/ksyssupervisor/moves"

#: How each filesystem's size can change, and the tool that does it; the
#: helper's table.
RESIZE = {
    "ext2": ("resize2fs", True, True), "ext3": ("resize2fs", True, True),
    "ext4": ("resize2fs", True, True), "ntfs": ("ntfsresize", True, True),
    "btrfs": ("btrfs", True, True), "xfs": ("xfs_growfs", True, False),
    "f2fs": ("resize.f2fs", True, False), "swap": ("mkswap", True, True),
}
NO_RESIZE = {
    "vfat": "FAT cannot be resized with the tools on this system. Copy "
            "the files off, delete the partition and create a new one of "
            "the size wanted.",
    "exfat": "exFAT cannot be resized: no tool for it exists. Copy the "
             "files off, delete the partition and create a new one of the "
             "size wanted.",
    "crypto_LUKS": "An encrypted volume is resized from inside, with the "
                   "tools of the system that uses it.",
    "LVM2_member": "An LVM volume is resized with LVM's own tools.",
}
PACKAGES = {
    "resize2fs": ("e2fsprogs",) * 3,
    "ntfsresize": ("ntfsprogs", "ntfs-3g", "ntfs-3g"),
    "btrfs": ("btrfs-progs",) * 3, "xfs_growfs": ("xfsprogs",) * 3,
    "resize.f2fs": ("f2fs-tools",) * 3, "mkswap": ("util-linux",) * 3,
}

LIVE = ("%s holds the running system (%s). Its partition cannot be changed "
        "while the system runs from it; start the computer from a live USB "
        "stick and use the Disk Utility from there.")


def _portable(disk):
    return (disk.removable or disk.hotplug
            or disk.kind in (disks.USB, disks.CARD))


def _missing(tool):
    fedora, debian, arch = PACKAGES[tool]
    return diskops.no(
        "The '%s' command is not installed.\n"
        "Fedora: sudo dnf install %s\n"
        "Debian/Ubuntu: sudo apt install %s\n"
        "Arch: sudo pacman -S %s" % (tool, fedora, debian, arch))


def logical_children(disk, volume):
    """The logical partitions inside an extended one."""
    if not volume.is_extended or volume.start is None:
        return []
    return [v for v in disk.volumes
            if v is not volume and v.start is not None
            and volume.start <= v.start < volume.start + volume.size]


def room(disk, volume):
    """(bytes free just before, bytes free just after) a partition, as far
    as the drive's unallocated stretches show it."""
    if volume.start is None:
        return 0, 0
    before = after = 0
    end = volume.start + volume.size
    for gap in disk.free:
        if 0 <= volume.start - (gap.start + gap.size) < 2 * MIB:
            before = gap.size
        if 0 <= gap.start - end < 2 * MIB:
            after = gap.size
    return before, after


def _is_partition(volume):
    if volume is None:
        return diskops.no("Select a partition for this.")
    if volume.kind != "part":
        return diskops.no("The filesystem sits on the bare drive, with no "
                          "partition table. Format the drive to give it "
                          "one.")
    return None


def availability(action, disk, volume=None, tools=diskfs.find_tool):
    if not disk.has_media:
        return diskops.no("There is no media in this device.")

    if action == diskops.NEW_TABLE:
        answer = diskops.availability(diskops.FORMAT, disk)
        return answer

    if action == diskops.FIND_LOST:
        if disk.read_only:
            return diskops.no("This drive is read-only, so a partition "
                              "found on it could not be put back.")
        if tools("testdisk") is None:
            return diskops.no(
                "The 'testdisk' command is not installed.\n"
                "Fedora: sudo dnf install testdisk\n"
                "Debian/Ubuntu: sudo apt install testdisk\n"
                "Arch: sudo pacman -S testdisk")
        return diskops.YES

    if action == diskops.PART_NEW:
        if disk.read_only:
            return diskops.no("This drive is read-only.")
        if disk.table not in ("gpt", "dos"):
            return diskops.no(
                "This drive has no partition table. Format the whole drive, "
                "or create a table first (New Partition Table).")
        if not disk.free:
            return diskops.no(
                "There is no unallocated space on this drive. Shrink or "
                "delete a partition first to make room.")
        return diskops.YES

    problem = _is_partition(volume)
    if problem is not None:
        return problem
    if volume.is_system:
        return diskops.no(LIVE % ("This volume",
                                  ", ".join(volume.system_mounts)))
    if disk.read_only or volume.read_only:
        return diskops.no("This drive is read-only.")
    if volume.inner:
        return diskops.no("This volume is open as an encrypted volume, LVM "
                          "or RAID. Close that first.")

    if action == diskops.PART_DELETE:
        if logical_children(disk, volume):
            return diskops.no("This extended partition still holds logical "
                              "partitions. Delete those first.")
        return diskops.YES

    if volume.is_extended:
        return diskops.no("An extended partition is only a container; "
                          "resize or move the partitions inside it.")
    state = diskfs.windows_state(volume)

    if action == diskops.PART_RESIZE:
        if state is not None:
            return diskops.no(state.text)
        if volume.fstype in NO_RESIZE:
            return diskops.no(NO_RESIZE[volume.fstype])
        if volume.fstype and volume.fstype not in RESIZE:
            return diskops.no("A %s volume cannot be resized from here."
                              % diskfs.title_of(volume.fstype))
        if volume.fstype:
            tool = RESIZE[volume.fstype][0]
            if tools(tool) is None:
                return _missing(tool)
        return diskops.YES

    if action == diskops.PART_MOVE:
        before, after = room(disk, volume)
        if not before and not after:
            return diskops.no(
                "There is no unallocated space next to this partition to "
                "move it into. Shrink or delete a neighbouring partition "
                "first.")
        return diskops.YES

    return diskops.no("Not available.")


def capability(volume):
    """One line on what resizing this filesystem allows."""
    fstype = volume.fstype
    if not fstype:
        return "No filesystem: only the partition changes size."
    if fstype not in RESIZE:
        return NO_RESIZE.get(fstype, "Cannot be resized.")
    _tool, _grow, shrink = RESIZE[fstype]
    name = diskfs.title_of(fstype)
    if shrink:
        return "%s can be made larger and smaller." % name
    return "%s can be made larger, but never smaller." % name


# ---- plans ----------------------------------------------------------------------

def _identity(identity, name):
    return identity if identity is not None else disks.identity(name)


def plan_table_new(disk, table, identity=None):
    plan = Plan(
        title="New partition table", target=describe_target(disk),
        loses=what_is_lost(disk),
        internal=not _portable(disk),
        confirm="Erase and Create the Table",
        steps=["Erase every signature on the drive",
               "Write an empty %s partition table"
               % ("GPT" if table == "gpt" else "MBR"),
               "Read it back and check it"],
        step_keys=["table", "verify"],
        request={"op": "table_new", "device": disk.path, "table": table,
                 "identity": _identity(identity, disk.name)})
    if any(v.mounted for v in disk.volumes):
        plan.steps.insert(0, "Unmount everything on the drive")
        plan.step_keys.insert(0, "unmount")
    plan.advice.append(Advice(
        INFO, "The drive is left empty",
        "A new table has no partitions. Create them afterwards with New "
        "Partition - or use Format on the whole drive, which does both in "
        "one go."))
    return plan


def plan_create(disk, start, size, fstype, label="", owner=True,
                identity=None, cluster=None):
    fs = diskfs.FILESYSTEMS.get(fstype) if fstype else None
    if fs is not None:
        label, problem = diskfs.validate_label(fs, label)
        if problem:
            raise ValueError(problem)
        diskops.check_cluster(fstype, size, disk, cluster)
    else:
        label, cluster = "", None
    plan = Plan(
        title="New partition",
        target="%s on %s" % (disks.format_size(size), describe_target(disk)),
        steps=["Add the partition to the table, in unallocated space"],
        step_keys=["table"],
        remount=_portable(disk) and fs is not None,
        request={"op": "part_create", "device": disk.path,
                 "start": int(start), "size": int(size),
                 "fstype": fstype or "", "label": label,
                 "owner": bool(owner and fs is not None and fs.native),
                 "cluster": cluster,
                 "identity": _identity(identity, disk.name)})
    if fs is not None:
        plan.steps += ["Create the %s filesystem%s%s" % (
            fs.title, " '%s'" % label if label else "",
            diskops.with_clusters(cluster)),
            "Read the result back and check it"]
        plan.step_keys += ["mkfs", "verify"]
        plan.advice = [a for a in diskfs.advise(fs, disk, size, None)
                       if "mounted automatically" not in a.title]
    return plan


def delete_advice(disk, volume):
    found = []
    parttype = (volume.parttype or "").lower()
    if parttype == disks.GPT_EFI:
        found.append(Advice(
            WARNING, "A system starts from this partition",
            "This is an EFI System partition: the computer reads it to "
            "start whatever system is installed on this drive - Windows "
            "included. Without it that system does not start."))
    elif parttype == disks.GPT_MSR:
        found.append(Advice(
            WARNING, "Windows keeps this partition for itself",
            "The Microsoft reserved partition holds nothing you can see, "
            "but Windows expects it on its drive."))
    if volume.fstab_mount:
        found.append(Advice(
            WARNING, "This volume is mounted automatically at startup",
            "/etc/fstab mounts it at %s. Remove that line afterwards - a "
            "system set up without 'nofail' on it stops during startup "
            "when the volume is not found." % volume.fstab_mount))
    found.append(Advice(
        INFO, "The contents are not overwritten",
        "Deleting removes the partition from the table; what was in it "
        "stays on the drive until something is written there, and can "
        "often be found again by the recovery tools. To make it "
        "unrecoverable, format it with 'Quick format' unticked first."))
    return found


def plan_delete(disk, volume, identity=None):
    plan = Plan(
        title="Delete partition", target=describe_target(disk, volume),
        loses=what_is_lost(disk, volume), advice=delete_advice(disk, volume),
        internal=not _portable(disk), confirm="Delete the Partition",
        steps=["Remove the partition from the table"], step_keys=["table"],
        request={"op": "part_delete", "device": volume.path,
                 "identity": _identity(identity, volume.name)})
    if volume.mounted:
        plan.steps.insert(0, "Unmount %s" % (volume.mountpoint
                                             or volume.name))
        plan.step_keys.insert(0, "unmount")
    return plan


def plan_limits(disk, volume, identity=None):
    return Plan(
        title="Reading the limits", target=describe_target(disk, volume),
        steps=["Ask the filesystem's own tool how small it can become, and "
               "the partition table how far it can grow. Nothing is "
               "changed."],
        step_keys=[],
        request={"op": "part_limits", "device": volume.path,
                 "identity": _identity(identity, volume.name)})


BACKUP = Advice(
    WARNING, "Have a copy of what matters first",
    "Changing a partition rewrites the structures everything on it "
    "depends on. It is checked before and after, and the previous "
    "partition table is saved, but a drive that fails or loses power "
    "partway can take the volume with it.")


def resize_advice(disk, volume, grow):
    found = [BACKUP]
    if volume.fstype == "ntfs":
        found.append(Advice(
            INFO, "Windows will check this drive when it next starts",
            "That is expected after a resize from outside Windows. Let it "
            "finish; it takes a moment on a healthy volume. Windows must "
            "have been shut down completely - not hibernated, and without "
            "'fast startup' - or the resize is refused with what to do."))
    if not grow and volume.fstype in ("ntfs",) and disk.holds_other_system:
        found.append(Advice(
            INFO, "Shrinking a Windows system volume",
            "Windows keeps a small recovery partition after its main "
            "volume. Shrinking frees room between the two, which is where "
            "a new partition can then go."))
    if volume.mounted and volume.fstype in ("ext2", "ext3", "ext4") \
            and not grow or volume.mounted and volume.fstype in (
                "ntfs", "f2fs", "swap"):
        found.append(Advice(
            INFO, "The volume is unmounted for this",
            "It is mounted again afterwards. Close anything that has "
            "files open on it."))
    return found


def plan_resize(disk, volume, size, identity=None):
    size = (int(size) // MIB) * MIB
    grow = size > volume.size
    offline = (volume.fstype in ("ntfs", "f2fs", "swap")
               or (volume.fstype in ("ext2", "ext3", "ext4")
                   and (not grow or not volume.mounted)))
    plan = Plan(
        title="Resize",
        target="%s to %s" % (describe_target(disk, volume),
                             disks.format_size(size)),
        advice=resize_advice(disk, volume, grow),
        remount=volume.mounted and offline,
        request={"op": "part_resize", "device": volume.path, "size": size,
                 "identity": _identity(identity, volume.name)})
    if offline and volume.mounted:
        plan.steps.append("Unmount %s" % (volume.mountpoint or volume.name))
        plan.step_keys.append("unmount")
    if offline and volume.fstype not in ("", "swap"):
        plan.steps.append("Check the filesystem; a damaged one is not "
                          "resized")
        plan.step_keys.append("check")
    part = ("Grow the partition in the table" if grow
            else "Shrink the partition in the table")
    fs = ("Grow the filesystem into the new space" if grow
          else "Shrink the filesystem to the new size")
    if not volume.fstype:
        plan.steps.append(part)
        plan.step_keys.append("table")
    elif grow:
        plan.steps += [part, fs]
        plan.step_keys += ["table", "filesystem"]
    else:
        plan.steps += [fs, part]
        plan.step_keys += ["filesystem", "table"]
    plan.steps.append("Check the result")
    plan.step_keys.append("verify")
    return plan


def move_advice(disk, volume):
    found = [Advice(
        WARNING, "This must not be interrupted",
        "Every block of the partition is copied to its new place - %s. "
        "It cannot be stopped once it has begun, and until it has "
        "finished the volume is not usable. If the power fails, what was "
        "done is on record and the move can be finished afterwards, but "
        "have a copy of what matters first."
        % disks.format_size(volume.size))]
    parttype = (volume.parttype or "").lower()
    if parttype == disks.GPT_EFI or volume.fstype == "ntfs" \
            and disk.holds_other_system:
        found.append(Advice(
            WARNING, "A system may not start after its partition has moved",
            "A system installed on this drive finds some of its parts by "
            "where they are. After a move it may need its start-up files "
            "repaired from its own installation or recovery media."))
    if volume.mounted:
        found.append(Advice(
            INFO, "The volume is unmounted for this",
            "It is mounted again afterwards. Close anything that has "
            "files open on it."))
    return found


def plan_move(disk, volume, start, identity=None):
    start = -(-int(start) // MIB) * MIB
    plan = Plan(
        title="Move",
        target="%s, %s %s" % (
            describe_target(disk, volume),
            disks.format_size(abs(start - (volume.start or 0))),
            "towards the start of the drive" if start < (volume.start or 0)
            else "towards the end of the drive"),
        advice=move_advice(disk, volume), remount=volume.mounted,
        request={"op": "part_move", "device": volume.path, "start": start,
                 "identity": _identity(identity, volume.name)})
    if volume.mounted:
        plan.steps.append("Unmount %s" % (volume.mountpoint or volume.name))
        plan.step_keys.append("unmount")
    if volume.fstype not in ("", "swap"):
        plan.steps.append("Check the filesystem; a damaged one is not moved")
        plan.step_keys.append("check")
    plan.steps += ["Copy every block to the new place",
                   "Point the partition table at it",
                   "Check the result"]
    plan.step_keys += ["move", "table", "verify"]
    return plan


# ---- a move that did not finish ----------------------------------------------------

def pending_moves(folder=None):
    """Records of partition moves that were cut short."""
    folder = folder or MOVES_DIR
    found = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return found
    for name in names:
        try:
            with open(os.path.join(folder, name), encoding="ascii") as handle:
                record = json.loads(handle.read(4096))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("version") == 1 \
                and isinstance(record.get("disk"), str) \
                and isinstance(record.get("number"), int):
            found.append(record)
    return found


def describe_move(record):
    return ("Partition %d of %s (%s) was being moved when the work was cut "
            "short. It is partly in its old place and partly in its new "
            "one, and must not be used until the move is finished."
            % (record["number"], record["disk"],
               disks.format_size(record.get("size", 0))))


def plan_resume(record):
    return Plan(
        title="Finish the move",
        target="partition %d of %s" % (record["number"], record["disk"]),
        steps=["Carry on copying from where it stopped",
               "Point the partition table at the new place"],
        step_keys=["move", "table"],
        request={"op": "move_resume", "disk": record["disk"],
                 "number": record["number"]})


# ---- results --------------------------------------------------------------------

def done_text(plan, result):
    op = plan.request.get("op")
    if op == "table_new":
        return "%s has a new, empty %s partition table." % (
            result.get("device", "The drive"),
            "GPT" if result.get("table") == "gpt" else "MBR")
    if op == "part_create":
        return "Created %s (%s%s)." % (
            result.get("device", "the partition"),
            disks.format_size(result.get("size", 0)),
            ", %s" % diskfs.title_of(result["fstype"])
            if result.get("fstype") else ", not formatted")
    if op == "part_delete":
        return "Deleted %s." % result.get("device", "the partition")
    if op == "part_resize":
        text = "Resized %s to %s." % (result.get("device", "the partition"),
                                      disks.format_size(result.get("size", 0)))
        if result.get("note"):
            text += " " + result["note"]
        return text
    if op in ("part_move", "move_resume"):
        return "Moved %s; it now starts %s into the drive." % (
            result.get("device") or "the partition",
            disks.format_size(result.get("start", 0)))
    return "%s finished." % plan.title


# ---- partitions that went missing ------------------------------------------------

def plan_search(disk, identity=None):
    return Plan(
        title="Search for lost partitions", target=describe_target(disk),
        steps=["Search the drive for the beginnings of filesystems. "
               "Nothing is changed.",
               "Check each one found, and leave out what collides with a "
               "partition that exists"],
        step_keys=["search", "confirm"],
        request={"op": "partition_search", "device": disk.path,
                 "identity": _identity(identity, disk.name)})


def describe_found(entry):
    """One line for a partition a search turned up."""
    name = diskfs.title_of(entry.get("fstype", ""))
    label = entry.get("label")
    return "%s%s, %s, starting %s into the drive" % (
        name, " '%s'" % label if label else "",
        disks.format_size(entry.get("size", 0)),
        disks.format_size(entry.get("start", 0)))


def plan_restore(disk, entry, identity=None):
    return Plan(
        preview={"fstype": entry.get("fstype", ""),
                 "label": entry.get("label", "")},
        title="Restore partition",
        target="%s on %s" % (describe_found(entry), disk.title),
        steps=["Write the partition's entry back into the table, at the "
               "place the filesystem was found. Nothing inside it is "
               "touched.", "Check that it reads as it should"],
        step_keys=["table", "verify"],
        request={"op": "partition_restore", "device": disk.path,
                 "start": int(entry["start"]), "size": int(entry["size"]),
                 "identity": _identity(identity, disk.name)})
