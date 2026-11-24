"""What can be put on a volume: the filesystems, their limits, and the advice
that goes with choosing one.

Data and small pure functions, so the format dialog's choices and warnings can
be tested without a dialog. The privileged helper keeps its own copy of the
hard limits - it cannot import this, and it must not take the client's word
for them - and a test holds the two together.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from . import disks

MIB = 1024 * 1024
GIB = 1024 * MIB
TIB = 1024 * GIB

#: Where the disk tools live. Searched directly rather than through PATH: on
#: Debian an ordinary user's PATH leaves out /usr/sbin, where every mkfs is,
#: and the helper that runs them looks in exactly these places.
TOOL_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")


def find_tool(*names):
    """Absolute path of the first of `names` that is installed, or None."""
    for name in names:
        for directory in TOOL_DIRS:
            path = os.path.join(directory, name)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None


@dataclass(frozen=True)
class Filesystem:
    key: str                 # what lsblk and blkid call it
    title: str
    works_with: str          # one line for the chooser
    mkfs: tuple              # the tool that creates it, under any of its names
    packages: tuple          # (Fedora, Debian/Ubuntu, Arch) package to install
    label_max: int
    label_counts: str        # "dos", "utf16" or "utf8": what label_max counts
    min_size: int
    max_size: Optional[int]
    native: bool             # has Unix ownership, so who owns the root matters
    keeps_uuid: bool         # the tool can be told which ID to give it
    label_spaces: bool = True

    def install_hint(self):
        fedora, debian, arch = self.packages
        return ("Fedora: sudo dnf install %s\n"
                "Debian/Ubuntu: sudo apt install %s\n"
                "Arch: sudo pacman -S %s" % (fedora, debian, arch))


FILESYSTEMS = {fs.key: fs for fs in (
    Filesystem(
        "exfat", "exFAT",
        "Windows, macOS, Linux, cameras, consoles. No file size limit.",
        ("mkfs.exfat",), ("exfatprogs", "exfatprogs", "exfatprogs"),
        11, "utf16", 8 * MIB, None, False, False),
    Filesystem(
        "vfat", "FAT32",
        "Read by practically everything. Files up to 4 GB each.",
        ("mkfs.fat", "mkfs.vfat"), ("dosfstools", "dosfstools", "dosfstools"),
        11, "dos", 34 * MIB, 2 * TIB, False, True),
    Filesystem(
        "ntfs", "NTFS",
        "Windows' own. Linux reads and writes it; macOS only reads it.",
        ("mkfs.ntfs", "mkntfs"), ("ntfsprogs", "ntfs-3g", "ntfs-3g"),
        32, "utf16", 8 * MIB, None, False, False),
    Filesystem(
        "ext4", "ext4",
        "Linux' standard. Not readable by Windows or macOS.",
        ("mkfs.ext4",), ("e2fsprogs", "e2fsprogs", "e2fsprogs"),
        16, "utf8", 8 * MIB, None, True, True),
    Filesystem(
        "btrfs", "Btrfs",
        "Linux, with snapshots and checksums. Not readable elsewhere.",
        ("mkfs.btrfs",), ("btrfs-progs", "btrfs-progs", "btrfs-progs"),
        255, "utf8", 128 * MIB, None, True, True),
    Filesystem(
        "xfs", "XFS",
        "Linux, built for large files. Cannot be shrunk later.",
        ("mkfs.xfs",), ("xfsprogs", "xfsprogs", "xfsprogs"),
        # No spaces: xfs_admin, the only tool that renames an XFS volume,
        # cannot write one, so such a name could never be changed again.
        12, "utf8", 300 * MIB, None, True, True, label_spaces=False),
    Filesystem(
        "f2fs", "F2FS",
        "Linux, designed for flash storage. Not readable elsewhere.",
        ("mkfs.f2fs",), ("f2fs-tools", "f2fs-tools", "f2fs-tools"),
        255, "utf16", 128 * MIB, None, True, True),
)}

#: Characters a FAT volume label may not contain.
FAT_FORBIDDEN = '*?.,;:/\\|+=<>[]"'

#: Up to this size a removable drive is given FAT32 by default, as Windows
#: does: it is the format every device made in the last thirty years reads.
FAT32_DEFAULT_LIMIT = 32 * GIB

#: An MBR partition table addresses 2^32 sectors.
MBR_SECTORS = 2 ** 32


def title_of(fstype):
    """A filesystem's display name, including ones that cannot be created."""
    if fstype in FILESYSTEMS:
        return FILESYSTEMS[fstype].title
    return {
        "": "Unformatted", "swap": "Swap", "crypto_LUKS": "Encrypted (LUKS)",
        "LVM2_member": "LVM", "linux_raid_member": "RAID member",
        "ext2": "ext2", "ext3": "ext3", "iso9660": "ISO 9660",
        "udf": "UDF", "hfsplus": "HFS+", "apfs": "APFS", "zfs_member": "ZFS",
        "BitLocker": "BitLocker",
    }.get(fstype, fstype)


# ---- labels ------------------------------------------------------------------

def label_length(fs, label):
    """How much of the limit `label` uses, in the unit the limit is in."""
    if fs.label_counts == "dos":
        return len(label.encode("cp850", "replace"))
    if fs.label_counts == "utf16":
        return len(label.encode("utf-16-le")) // 2
    return len(label.encode("utf-8"))


def validate_label(fs, label):
    """(label as it will be written, None) or (label, what is wrong).

    FAT labels are stored upper-case, so the returned label is what the
    volume will really be called.
    """
    label = label.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in label):
        return label, "The name contains a character that cannot be used."
    if fs.label_counts == "dos":
        label = label.upper()
        bad = sorted(set(c for c in label if c in FAT_FORBIDDEN))
        if bad:
            return label, ("A FAT name cannot contain %s" % " ".join(bad))
        try:
            label.encode("cp850")
        except UnicodeEncodeError:
            return label, ("A FAT name can only use Western European "
                           "characters.")
    if label.startswith("-"):
        return label, "The name cannot start with a dash."
    used = label_length(fs, label)
    if used > fs.label_max:
        return label, ("Too long: %s allows %d characters, this is %d."
                       % (fs.title, fs.label_max, used))
    if not fs.label_spaces and " " in label:
        return label, "%s names cannot contain a space." % fs.title
    return label, None


# ---- choosing ----------------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    filesystem: Filesystem
    available: bool
    reason: str = ""           # why not, when it is not
    recommended: bool = False


def recommended(disk, size):
    """The filesystem to offer first for a target of `size` bytes."""
    if disk.kind in (disks.USB, disks.CARD) or disk.removable:
        return "vfat" if size <= FAT32_DEFAULT_LIMIT else "exfat"
    return "ext4"


def choices(disk, size, tools=find_tool):
    """Every filesystem, with whether it can be made here and why not.

    Unavailable ones stay in the list: a greyed entry that says "install
    exfatprogs" is more use than a missing one.
    """
    best = recommended(disk, size)
    found = []
    for fs in FILESYSTEMS.values():
        reason = ""
        if size < fs.min_size:
            reason = "Needs at least %s." % disks.format_size(fs.min_size)
        elif fs.max_size is not None and size > fs.max_size:
            reason = ("%s cannot be larger than %s."
                      % (fs.title, disks.format_size(fs.max_size)))
        elif tools(*fs.mkfs) is None:
            reason = ("The tool that creates %s is not installed.\n%s"
                      % (fs.title, fs.install_hint()))
        found.append(Choice(fs, not reason, reason, fs.key == best))
    # The recommended one first if it can be made; the rest keep their order.
    found.sort(key=lambda c: 0 if (c.recommended and c.available) else 1)
    return found


def default_table(disk):
    """The partition table a freshly formatted drive gets.

    Removable drives get MBR while it can describe them: cameras, televisions,
    car stereos and old firmware read nothing else. Everything else gets GPT.
    """
    if not table_allowed(disk, "dos"):
        return "gpt"
    if disk.kind in (disks.USB, disks.CARD) or disk.removable:
        return "dos"
    return "gpt"


def table_allowed(disk, table):
    if table == "dos":
        return disk.size <= MBR_SECTORS * disk.logical_sector
    return table == "gpt"


# ---- cluster size ------------------------------------------------------------
#
# A file always takes whole clusters, so the cluster size is how much space
# the smallest file uses up. Only the three Windows filesystems let it be
# chosen; the limits below are held to the helper's own copy by a test.

KIB = 1024

#: The smallest and largest cluster each filesystem can be given here: what
#: Windows offers, and what the Linux tools accept. Never below the drive's
#: logical sector either.
CLUSTER_RANGE = {
    "exfat": (512, 32 * MIB),
    "vfat": (512, 64 * KIB),
    "ntfs": (512, 2 * MIB),
}

#: Below this mkfs.fat is not asked for FAT32 and makes FAT16, which picks
#: its own cluster size.
FAT32_FROM = 64 * MIB

#: The cluster counts FAT32 allows, with a margin at both ends. Below 65525
#: clusters mkfs.fat still writes "FAT32" (it only warns), but Windows tells
#: FAT types apart by the count and would read such a volume as FAT16; the
#: margin covers the few clusters the estimate is off by.
FAT32_MIN_CLUSTERS = 65525 + 256
FAT32_MAX_CLUSTERS = 0x0FFFFFF5 - 65536

#: NTFS counts clusters in 32 bits. exFAT could too, but it keeps 4 bytes of
#: table for each one: capped where that table reaches 1 GiB (8 KiB clusters
#: on a 2 TB drive are still allowed).
CLUSTER_MAX_COUNT = {"exfat": 2 ** 28, "ntfs": 2 ** 32 - 1}

#: A cluster larger than this share of the volume is of no use, and mkntfs
#: fails on a volume of only a few clusters.
CLUSTER_MIN_COUNT = 256


def cluster_valid(fstype, size, logical_sector, cluster):
    """Whether a `size`-byte volume of `fstype` can have `cluster` clusters."""
    if fstype not in CLUSTER_RANGE or cluster <= 0 \
            or cluster & (cluster - 1):
        return False
    low, high = CLUSTER_RANGE[fstype]
    if cluster < max(low, logical_sector) or cluster > high:
        return False
    if fstype == "vfat":
        if size < FAT32_FROM:
            return False
        # The reserved sectors, then two tables of 4 bytes a cluster, then
        # the clusters, aligned to a cluster: within a few clusters of what
        # mkfs.fat makes.
        count = (size - 32 * logical_sector - cluster) // (cluster + 8)
        return FAT32_MIN_CLUSTERS <= count <= FAT32_MAX_CLUSTERS
    count = size // cluster
    return CLUSTER_MIN_COUNT <= count <= CLUSTER_MAX_COUNT[fstype]


def usual_cluster(fstype, size):
    """The cluster size Windows gives a volume of this size by default."""
    if fstype == "exfat":
        if size <= 256 * MIB:
            return 4 * KIB
        return 32 * KIB if size <= 32 * GIB else 128 * KIB
    if fstype == "vfat":
        for limit, cluster in ((64 * MIB, 512), (128 * MIB, KIB),
                               (256 * MIB, 2 * KIB), (8 * GIB, 4 * KIB),
                               (16 * GIB, 8 * KIB), (32 * GIB, 16 * KIB)):
            if size <= limit:
                return cluster
        return 32 * KIB
    if fstype == "ntfs":
        # 4 KiB up to 16 TiB, then whatever keeps the count in 32 bits.
        cluster = 4 * KIB
        while size > (2 ** 32 - 1) * cluster and cluster < 2 * MIB:
            cluster *= 2
        return cluster
    return None


def format_cluster(size):
    if size >= MIB:
        return "%d MiB" % (size // MIB)
    if size >= KIB:
        return "%d KiB" % (size // KIB)
    return "%d bytes" % size


@dataclass(frozen=True)
class ClusterChoice:
    """The cluster sizes a new volume can have, ascending.

    Empty `sizes` means nothing can be chosen: `fixed` is then what the
    field shows and `reason` says why.
    """
    sizes: tuple = ()
    recommended: Optional[int] = None
    fixed: str = ""
    reason: str = ""


#: Why a Linux filesystem gets no choice.
LINUX_BLOCKS = (
    "Linux filesystems use 4 KiB, the size of the memory pages the system "
    "works in. Other sizes gain nothing in everyday use, and some cannot be "
    "mounted on every kernel.")


def cluster_choice(fstype, size, logical_sector=512):
    """The cluster sizes a `size`-byte volume of `fstype` can be given."""
    if fstype in CLUSTER_RANGE:
        if fstype == "vfat" and size < FAT32_FROM:
            return ClusterChoice(
                fixed="Automatic",
                reason="A volume this small becomes FAT16, which chooses "
                       "its own cluster size.")
        sizes = []
        cluster = 512
        while cluster <= CLUSTER_RANGE[fstype][1]:
            if cluster_valid(fstype, size, logical_sector, cluster):
                sizes.append(cluster)
            cluster *= 2
        if not sizes:
            return ClusterChoice(fixed="Automatic",
                                 reason="No size fits this volume exactly; "
                                        "the tool chooses one.")
        usual = usual_cluster(fstype, size)
        # The usual one where this volume can have it, else the nearest.
        best = usual if usual in sizes else min(
            sizes, key=lambda c: (abs(c.bit_length() - usual.bit_length()),
                                  -c))
        return ClusterChoice(tuple(sizes), best)
    reason = LINUX_BLOCKS
    if fstype == "btrfs":
        reason += " Btrfs keeps very small files inside its own records, " \
                  "where they take no block of their own."
    return ClusterChoice(fixed="4 KiB", reason=reason)


def cluster_note(fstype, cluster, recommended):
    """What choosing `cluster` means, for the line under the field."""
    # No-break spaces: wrapped between number and unit, "128 / KiB" read
    # as two things.
    text = ("Every file takes whole clusters: even a 1\u00a0KB file uses %s."
            % format_cluster(cluster).replace(" ", "\u00a0"))
    if cluster == recommended:
        text += " The usual size for a volume this large."
    elif cluster > recommended:
        text += (" Larger than usual: many small files waste a lot of "
                 "space, and large files are not noticeably faster.")
    else:
        text += (" Smaller than usual: less space lost to small files, "
                 "at the cost of a little speed with very large ones.")
    if fstype == "ntfs" and cluster > 4 * KIB:
        text += " NTFS cannot compress files with clusters this large."
    if fstype == "ntfs" and cluster > 64 * KIB:
        text += (" Windows before Windows 10 version 1709 cannot open "
                 "the volume.")
    if fstype == "vfat" and cluster > 32 * KIB:
        text += " Some older devices and programs cannot read it."
    return text


# ---- advice ------------------------------------------------------------------

WARNING = "warning"
INFO = "info"


@dataclass(frozen=True)
class Advice:
    level: str
    title: str
    text: str


def advise(fs, disk, size, volume=None, keeps_identity=False):
    """What the user should know before formatting this target as `fs`.

    `volume` is the partition being formatted, or None for the whole drive.
    `keeps_identity` says the new filesystem will get the old one's ID.
    """
    found = []
    portable = disk.kind in (disks.USB, disks.CARD) or disk.removable

    if fs.key == "ntfs" and portable:
        if size <= FAT32_DEFAULT_LIMIT:
            found.append(Advice(
                WARNING, "NTFS is a poor choice for a small removable drive",
                "NTFS keeps a journal, which means extra writes that wear "
                "flash memory and slow a memory stick down. Televisions, "
                "cameras, car stereos and consoles usually cannot read it, "
                "macOS cannot write to it, and it must be ejected properly "
                "every time. exFAT is the better fit here, or FAT32 for "
                "the widest compatibility."))
        else:
            found.append(Advice(
                WARNING, "Many devices cannot read NTFS",
                "Televisions, cameras, car stereos and consoles usually "
                "cannot read NTFS, and macOS cannot write to it. If this "
                "drive is only for Windows and Linux computers it is fine; "
                "otherwise choose exFAT."))

    if fs.native and portable:
        found.append(Advice(
            WARNING, "Only Linux will be able to read this drive",
            "Windows and macOS cannot open %s. Choose exFAT if the drive "
            "has to work on other systems too." % fs.title))

    if fs.key == "vfat":
        found.append(Advice(
            INFO, "Files larger than 4 GB will not fit",
            "FAT32 cannot store a single file of 4 GB or more - a long "
            "video or a disc image, for example. exFAT has no such limit."))
        if size > FAT32_DEFAULT_LIMIT:
            found.append(Advice(
                INFO, "Windows cannot create FAT32 this large",
                "Windows reads and writes it without trouble, but its own "
                "format tool refuses FAT32 above 32 GB, so the drive could "
                "not be reformatted this way from there."))

    if volume is not None and portable:
        layout = split_up(disk)
        if layout:
            found.append(layout)

    target = volume
    if target is None:
        mounted = [v for v in disk.volumes if v.fstab_mount]
        if mounted:
            found.append(Advice(
                WARNING, "This drive is mounted automatically at startup",
                "/etc/fstab mounts %s from it. Formatting the whole drive "
                "removes what those lines refer to, so they have to be "
                "removed or updated afterwards - a system set up without "
                "'nofail' on such a line stops during startup when the "
                "volume is missing."
                % ", ".join(sorted(set(v.fstab_mount for v in mounted)))))
    elif target.fstab_mount:
        if keeps_identity:
            found.append(Advice(
                INFO, "Automatic mounting keeps working",
                "/etc/fstab mounts this volume at %s. The new filesystem "
                "is given the same ID, so that line stays valid."
                % target.fstab_mount))
        else:
            found.append(Advice(
                WARNING, "This volume is mounted automatically at startup",
                "/etc/fstab mounts it at %s by its ID and filesystem type, "
                "and formatting it as %s changes those. Update or remove "
                "that line afterwards - a system set up without 'nofail' "
                "on it stops during startup when the volume is not found."
                % (target.fstab_mount, fs.title)))
    return found


# ---- volumes only Windows can work on ----------------------------------------

#: Partition types of Windows' own volume managers.
LDM_TYPES = frozenset(("5808c8aa-7e8f-42e0-85d2-e1e90434cfb3",
                       "af9b60a0-1431-4f62-bc68-3311714a69ad", "0x42"))
STORAGE_SPACES_TYPE = "e75caf8f-f680-4cee-afa3-b001e56efc2d"

BITLOCKER = Advice(
    WARNING, "Encrypted by Windows (BitLocker)",
    "This volume is encrypted by Windows, so nothing outside Windows can "
    "read what is on it. It can be formatted or copied exactly as it is, "
    "but not checked, resized, defragmented or searched for files.\n\n"
    "To work on its contents: in Windows, open Settings > Privacy & "
    "security > Device encryption (or 'Manage BitLocker' in the Control "
    "Panel), turn encryption off and wait until it has finished.")
DYNAMIC_DISK = Advice(
    WARNING, "A Windows dynamic disk",
    "This partition belongs to Windows' own volume manager, which only "
    "Windows understands. Use Disk Management in Windows to change it.")
STORAGE_SPACE = Advice(
    WARNING, "Part of a Windows Storage Space",
    "This partition is part of a storage pool that only Windows can put "
    "together. Use 'Storage Spaces' in Windows to change it.")
REFS = Advice(
    WARNING, "ReFS, a Windows-only filesystem",
    "Linux cannot read or write ReFS. Use Windows to work on this volume.")


def windows_state(volume):
    """What makes a volume one that only Windows can work on, as an Advice
    with what to do about it, or None.

    Only what can be told without opening the device: its type as the
    system's own probe reports it, and the partition type. Whether an NTFS
    volume was left hibernated needs a look inside, which the privileged
    helper takes at the moment it matters.
    """
    if volume is None:
        return None
    fstype = (volume.fstype or "").lower()
    parttype = (volume.parttype or "").lower()
    if fstype == "bitlocker":
        return BITLOCKER
    if fstype == "refs":
        return REFS
    if parttype in LDM_TYPES or fstype == "ldm_data":
        return DYNAMIC_DISK
    if parttype == STORAGE_SPACES_TYPE:
        return STORAGE_SPACE
    return None


#: Unallocated room that is worth pointing out: this much, or this share of
#: the drive, whichever is smaller.
NOTABLE_GAP = 1024 ** 3
NOTABLE_SHARE = 0.05


def split_up(disk):
    """Advice for formatting one partition of a drive that is not one piece.

    An installer image, several partitions, or room no partition uses: in
    each case formatting a single partition leaves the drive smaller than it
    is, and only the whole-drive format puts it back together.
    """
    full = disks.format_size(disk.size)
    if disk.written_as_image:
        return Advice(
            WARNING, "This drive was written as an installer image",
            "Its layout comes from that image, which is why it shows less "
            "than its real %s. Formatting one partition leaves it that way. "
            "Choose 'Whole drive' to get all of it back as one volume."
            % full)
    parts = disk.partitions
    gap = disk.unallocated
    notable = gap >= min(NOTABLE_GAP, disk.size * NOTABLE_SHARE)
    if len(parts) > 1 or (parts and notable):
        what = ("%d partitions" % len(parts) if len(parts) > 1
                else "one partition")
        if notable:
            what += " and %s that no partition uses" % disks.format_size(gap)
        return Advice(
            INFO, "This drive is split up",
            "It has %s. Formatting this partition leaves the rest as it is. "
            "Choose 'Whole drive' for a single volume of the full %s."
            % (what, full))
    return None


def can_keep_identity(fs, volume):
    """Whether a reformat can give the new filesystem the old one's ID.

    Only when the type stays the same and the tool can be told an ID: then
    whatever mounts the volume by that ID keeps working untouched.
    """
    return (volume is not None and fs.keeps_uuid and bool(volume.uuid)
            and volume.fstype == fs.key)
