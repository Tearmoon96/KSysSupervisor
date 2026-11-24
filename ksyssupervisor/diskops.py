"""What can be done to a drive or volume, and what doing it involves.

Two questions the window asks before it shows anything: is this action
available here, and if not, why - so that a greyed-out button can say what
is in the way - and what exactly would happen, step by step, so that the
confirmation shows the real sequence and what will be lost.

The answers are advice to the user interface, not the safety mechanism: the
privileged helper makes every one of these decisions again for itself.

Qt-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import diskfs, disks

# Actions, by the names the window's buttons carry.
FORMAT = "format"
MOUNT = "mount"
UNMOUNT = "unmount"
EJECT = "eject"
RENAME = "rename"
# Looking after a volume; the rules for these are in diskmaintain.
CHECK = "check"
TRIM = "trim"
ANALYSE = "analyse"
DEFRAG = "defrag"
WIPE_FREE = "wipe_free"
MAINTENANCE = frozenset((CHECK, TRIM, ANALYSE, DEFRAG, WIPE_FREE))
# Copying; the rules for these are in diskcopy.
CLONE = "clone"
IMAGE_SAVE = "image_save"
IMAGE_WRITE = "image_write"
RESCUE = "rescue"
IMAGE_CLOSE = "image_close"
RECOVERY_STICK = "recovery_stick"
COPYING = frozenset((CLONE, IMAGE_SAVE, IMAGE_WRITE, RESCUE, IMAGE_CLOSE,
                     RECOVERY_STICK))
# Editing the partition table; the rules for these are in diskparts.
NEW_TABLE = "new_table"
PART_NEW = "part_new"
PART_DELETE = "part_delete"
PART_RESIZE = "part_resize"
PART_MOVE = "part_move"
FIND_LOST = "find_lost"
PARTITIONING = frozenset((NEW_TABLE, PART_NEW, PART_DELETE, PART_RESIZE,
                          PART_MOVE, FIND_LOST))
# Looking for lost files; the rules are in diskrecover.
RECOVER = "recover"

#: Filesystems whose label can be rewritten in place, and the tool that does.
LABEL_TOOLS = {
    "ext2": "e2label", "ext3": "e2label", "ext4": "e2label",
    "vfat": "fatlabel", "exfat": "exfatlabel", "ntfs": "ntfslabel",
    "btrfs": "btrfs", "xfs": "xfs_admin", "swap": "swaplabel",
}

#: The package each of those tools comes in, (Fedora, Debian/Ubuntu, Arch).
LABEL_PACKAGES = {
    "e2label": ("e2fsprogs",) * 3,
    "fatlabel": ("dosfstools",) * 3,
    "exfatlabel": ("exfatprogs",) * 3,
    "ntfslabel": ("ntfsprogs", "ntfs-3g", "ntfs-3g"),
    "btrfs": ("btrfs-progs",) * 3,
    "xfs_admin": ("xfsprogs",) * 3,
    "swaplabel": ("util-linux",) * 3,
}

#: Filesystem types that are not something to mount: containers and members.
NOT_MOUNTABLE = frozenset(("swap", "crypto_LUKS", "LVM2_member",
                           "linux_raid_member", "zfs_member", "BitLocker"))


@dataclass(frozen=True)
class Availability:
    enabled: bool
    reason: str = ""           # why not; shown as the greyed button's tooltip

    def __bool__(self):
        return self.enabled


YES = Availability(True)


def no(reason):
    return Availability(False, reason)


def _system_reason(what, mounts):
    listed = ", ".join(sorted(set(mounts)))
    return ("%s holds the running system (%s). It cannot be changed while "
            "the system runs from it." % (what, listed))


def _stacked(volume):
    """Whether another storage layer is open on top of the volume."""
    return bool(volume.inner)


def availability(action, disk, volume=None, tools=diskfs.find_tool):
    """Whether `action` can be done to `volume`, or to `disk` when None."""
    if action in MAINTENANCE:
        from . import diskmaintain
        return diskmaintain.availability(action, disk, volume, tools)
    if action in COPYING:
        from . import diskcopy
        return diskcopy.availability(action, disk, volume)
    if action in PARTITIONING:
        from . import diskparts
        return diskparts.availability(action, disk, volume, tools)
    if action == RECOVER:
        from . import diskrecover
        return diskrecover.availability(disk, volume, tools)
    if action in (MOUNT, UNMOUNT, EJECT):
        # On the recovery stick, a prepared change: what the picture shows
        # is not on the drive yet, and these act on the drive right away.
        if volume is not None and volume.pending:
            return no("This is how the volume will be after Apply Changes; "
                      "it is not on the drive yet.")
        if volume is None and disk.pending:
            return no("Changes are prepared for this drive. Apply them, or "
                      "remove them from the list, first.")
    if not disk.has_media:
        return no("There is no media in this device.")
    if volume is None:
        return _disk_availability(action, disk)
    return _volume_availability(action, disk, volume, tools)


def _disk_availability(action, disk):
    if action == FORMAT:
        if disk.is_system:
            mounts = [m for v in disk.system_volumes for m in v.system_mounts]
            return no(_system_reason("This drive", mounts))
        if disk.read_only:
            return no("This drive is read-only: a write-protect switch is "
                      "set, or the drive has locked itself.")
        if any(_stacked(v) for v in disk.volumes):
            return no("Part of this drive is open as an encrypted volume, "
                      "LVM or RAID. Close that first.")
        if disk.size < min(fs.min_size for fs
                           in diskfs.FILESYSTEMS.values()) + 2 * disks.MIB:
            return no("This drive is too small to hold a filesystem.")
        return YES

    if action == EJECT:
        if disk.is_system:
            return no("The running system is on this drive.")
        if not (disk.removable or disk.hotplug or disk.transport == "usb"):
            return no("This is an internal drive. Only removable and "
                      "hot-pluggable drives are ejected.")
        return YES

    return no("Select a volume for this.")


def _volume_availability(action, disk, volume, tools):
    if action == FORMAT:
        if volume.is_system:
            return no(_system_reason("This volume", volume.system_mounts))
        if volume.kind == "whole":
            # The filesystem sits on the bare drive: formatting "it" and
            # formatting the drive are one and the same thing.
            return _disk_availability(action, disk)
        if volume.is_extended:
            return no("An extended partition is only a container for "
                      "others; there is nothing in it to format.")
        if _stacked(volume):
            return no("This volume is open as an encrypted volume, LVM or "
                      "RAID. Close that first.")
        if disk.read_only or volume.read_only:
            return no("This volume is read-only.")
        if volume.size < min(fs.min_size
                             for fs in diskfs.FILESYSTEMS.values()):
            return no("This partition is too small to hold a filesystem.")
        return YES

    if action == MOUNT:
        if volume.mounted:
            return no("Already mounted at %s."
                      % (volume.mountpoint or "a system location"))
        if volume.is_extended or not volume.fstype:
            return no("There is no filesystem here to mount.")
        if volume.is_encrypted:
            return no("This volume is encrypted. Unlock it from the file "
                      "manager, which asks for its passphrase.")
        if volume.fstype in NOT_MOUNTABLE:
            return no("%s is not something that can be mounted."
                      % diskfs.title_of(volume.fstype))
        return YES

    if action == UNMOUNT:
        if not volume.mounted:
            return no("Not mounted.")
        if volume.is_system:
            return no(_system_reason("This volume", volume.system_mounts))
        return YES

    if action == RENAME:
        if volume.is_system:
            return no("This volume holds the running system (%s), which may "
                      "find it by its name."
                      % ", ".join(volume.system_mounts))
        if _stacked(volume):
            return no("This volume is open as an encrypted volume, LVM or "
                      "RAID. Close that first.")
        tool = LABEL_TOOLS.get(volume.fstype)
        if tool is None:
            return no("The name of %s cannot be changed from here."
                      % ("a %s volume" % diskfs.title_of(volume.fstype)
                         if volume.fstype else "an unformatted partition"))
        if disk.read_only or volume.read_only:
            return no("This volume is read-only.")
        if tools(tool) is None:
            fedora, debian, arch = LABEL_PACKAGES[tool]
            return no("The '%s' command is not installed.\n"
                      "Fedora: sudo dnf install %s\n"
                      "Debian/Ubuntu: sudo apt install %s\n"
                      "Arch: sudo pacman -S %s" % (tool, fedora, debian, arch))
        return YES

    if action == EJECT:
        return _disk_availability(EJECT, disk)

    return no("Not available.")


# ---- plans -------------------------------------------------------------------

@dataclass
class Plan:
    """An operation worked out but not started: what the confirmation shows
    and what the helper is asked to do."""
    title: str
    target: str                          # "SanDisk Ultra (sdd), 32 GB"
    steps: list = field(default_factory=list)       # human-readable, in order
    step_keys: list = field(default_factory=list)   # the helper's step names
    loses: list = field(default_factory=list)       # what will be destroyed
    advice: list = field(default_factory=list)      # diskfs.Advice
    request: dict = field(default_factory=dict)     # for the helper
    remount: bool = False                # mount the result again afterwards
    internal: bool = False               # not removable: confirm harder
    confirm: str = ""                    # the confirming button, if not the
    #                                      usual wording
    #: What the recovery stick's picture needs to show the result before it
    #: happens and the request does not say (a restored partition's
    #: filesystem).
    preview: dict = field(default_factory=dict)


#: Full erase methods. A drive that takes TRIM is discarded: the same result
#: in seconds and without the wear of writing every block.
ERASE_NONE = "none"
ERASE_ZERO = "zero"
ERASE_DISCARD = "discard"


def full_erase_method(disk):
    return ERASE_DISCARD if (disk.discard and disk.is_solid_state) \
        else ERASE_ZERO


def describe_target(disk, volume=None):
    if volume is None:
        return "%s (%s), %s" % (disk.title, disk.name,
                                disks.format_size(disk.size))
    return "%s (%s), %s on %s" % (volume.title, volume.name,
                                  disks.format_size(volume.size), disk.title)


def what_is_lost(disk, volume=None):
    """One line per thing a format destroys, most informative first."""
    targets = [volume] if volume is not None else list(disk.volumes)
    lines = []
    for each in targets:
        if each.is_extended:
            continue
        parts = [each.title]
        if each.fstype:
            parts.append(diskfs.title_of(each.fstype))
        if each.used is not None:
            parts.append("%s of data" % disks.format_size(each.used))
        else:
            parts.append(disks.format_size(each.size))
        lines.append(" - ".join(parts))
    if volume is None and disk.holds_other_system:
        lines.insert(0, "Another operating system that starts from this "
                        "drive (it has its own boot partition)")
    if not lines:
        lines.append("Everything on %s" % disk.title)
    return lines


def plan_format(disk, volume, fs_key, label, quick=True, table=None,
                take_ownership=True, identity=None, cluster=None):
    """Work out a format of `volume`, or of the whole `disk` when None.

    `identity` is disks.identity() of the target, read when the window drew
    it; the helper checks it against the device it finds. `cluster` is the
    cluster size in bytes, or None for the tool's own choice.
    """
    fs = diskfs.FILESYSTEMS[fs_key]
    whole = volume is None
    size = disk.size - 2 * disks.MIB if whole else volume.size
    label, problem = diskfs.validate_label(fs, label)
    if problem:
        raise ValueError(problem)
    check_cluster(fs_key, size, disk, cluster)
    if whole:
        table = table or diskfs.default_table(disk)
        if not diskfs.table_allowed(disk, table):
            raise ValueError("An MBR partition table cannot describe a "
                             "drive this large.")
    else:
        table = None

    erase = ERASE_NONE if quick else full_erase_method(disk)
    keep = diskfs.can_keep_identity(fs, volume)
    name = disk.name if whole else volume.name
    mounted = (disk.mounted_volumes if whole
               else ((volume,) if volume.mounted else ()))
    portable = (disk.removable or disk.hotplug
                or disk.kind in (disks.USB, disks.CARD))

    plan = Plan(
        title="Format",
        target=describe_target(disk, volume),
        loses=what_is_lost(disk, volume),
        advice=diskfs.advise(fs, disk, size, volume, keeps_identity=keep),
        internal=not portable,
        # Ready to use afterwards where it was in use before, and on a drive
        # that is plugged in to be used; an internal volume nobody had
        # mounted is left as it was found.
        remount=bool(mounted) or portable,
    )

    if mounted:
        plan.steps.append("Unmount %s" % ", ".join(
            v.mountpoint or v.name for v in mounted))
        plan.step_keys.append("unmount")
    if erase == ERASE_ZERO:
        plan.steps.append("Overwrite all %s with zeros - slow, and it "
                          "cannot be undone" % disks.format_size(
                              disk.size if whole else volume.size))
    elif erase == ERASE_DISCARD:
        plan.steps.append("Discard every block (TRIM), which makes the old "
                          "contents unrecoverable on this kind of drive")
    else:
        plan.steps.append("Erase the old filesystem signatures")
    plan.step_keys.append("erase")
    if whole:
        plan.steps.append("Create a new %s partition table with one "
                          "partition" % ("GPT" if table == "gpt" else "MBR"))
        plan.step_keys.append("table")
    plan.steps.append("Create the %s filesystem%s%s"
                      % (fs.title, " '%s'" % label if label else "",
                         with_clusters(cluster)))
    plan.step_keys.append("mkfs")
    plan.steps.append("Read the result back and check it")
    plan.step_keys.append("verify")

    plan.request = {
        "op": "format",
        "device": "/dev/%s" % name,
        "identity": identity if identity is not None
        else disks.identity(name),
        "fstype": fs.key,
        "label": label,
        "erase": erase,
        "table": table,
        "owner": bool(take_ownership and fs.native),
        "uuid": volume.uuid if keep else None,
        "cluster": cluster,
    }
    return plan


def check_cluster(fs_key, size, disk, cluster):
    """Refuse a cluster size the volume cannot have (ValueError)."""
    if cluster is not None and not diskfs.cluster_valid(
            fs_key, size, disk.logical_sector, cluster):
        raise ValueError("A %s volume of this size cannot have clusters of "
                         "%s." % (diskfs.title_of(fs_key),
                                  diskfs.format_cluster(cluster)))


def with_clusters(cluster):
    """The end of a "Create the ... filesystem" step."""
    return (" with %s clusters" % diskfs.format_cluster(cluster)
            if cluster else "")


def plan_rename(disk, volume, label, identity=None):
    """Work out a change of `volume`'s label."""
    fs = diskfs.FILESYSTEMS.get(
        "ext4" if volume.fstype in ("ext2", "ext3") else volume.fstype)
    if fs is not None:
        label, problem = diskfs.validate_label(fs, label)
        if problem:
            raise ValueError(problem)
    else:
        label = label.strip()

    plan = Plan(title="Rename", target=describe_target(disk, volume),
                remount=volume.mounted)
    if volume.mounted:
        plan.steps.append("Unmount %s" % (volume.mountpoint or volume.name))
        plan.step_keys.append("unmount")
    plan.steps.append("Write the new name%s"
                      % (" '%s'" % label if label else " (none)"))
    plan.step_keys.append("label")
    plan.steps.append("Read the result back and check it")
    plan.step_keys.append("verify")
    if volume.mounted:
        plan.steps.append("Mount it again")
    plan.request = {
        "op": "label",
        "device": volume.path,
        "identity": identity if identity is not None
        else disks.identity(volume.name),
        "label": label,
    }
    return plan
