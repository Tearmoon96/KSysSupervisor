"""Block devices: what is connected, what is on it, and what the system needs.

The inventory behind the disk utility. Everything here is read without
privileges: `lsblk` for what udev already knows about each device (filesystem,
label, mount points, usage), sysfs for what the kernel itself publishes (size,
partition offsets, sector sizes, discard support, the disk sequence number) and
/etc/fstab for what the system mounts on its own at startup.

Qt-free, like the sensor providers: the roots and the lsblk runner are module
level so the tests can point them at a fixture.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import Optional

from . import log

# Module level so tests can point these at a fixture tree.
SYS_CLASS_BLOCK = "/sys/class/block"
FSTAB = "/etc/fstab"

MIB = 1024 * 1024

#: The smallest unallocated stretch worth showing. Partitioning tools leave a
#: MiB or so after the last partition as a matter of course; that is rounding,
#: not room for anything.
MIN_FREE = 2 * MIB

#: Columns asked of lsblk. Named one by one rather than --output-all: the full
#: set changes between util-linux releases and costs a probe per column.
LSBLK_COLUMNS = (
    "NAME", "KNAME", "PATH", "PKNAME", "TYPE", "SIZE", "FSTYPE", "FSVER",
    "LABEL", "UUID", "PARTLABEL", "PARTTYPE", "PARTTYPENAME", "PARTUUID",
    "PARTN", "PTTYPE", "PTUUID", "MOUNTPOINTS", "FSUSED", "FSAVAIL", "FSSIZE",
    "MODEL", "VENDOR", "SERIAL", "WWN", "REV", "TRAN", "ROTA", "RM", "HOTPLUG",
    "RO", "START", "PHY-SEC", "LOG-SEC", "DISC-GRAN", "DISC-MAX",
)

#: What an older lsblk still understands. An unknown column is an error there,
#: not an empty value, so the request is repeated with this set and the rest
#: comes from sysfs.
LSBLK_FALLBACK = (
    "NAME", "KNAME", "PATH", "PKNAME", "TYPE", "SIZE", "FSTYPE", "LABEL",
    "UUID", "PARTLABEL", "PARTTYPE", "PARTUUID", "PTTYPE", "MOUNTPOINT",
    "FSUSED", "FSAVAIL", "FSSIZE", "MODEL", "VENDOR", "SERIAL", "WWN", "TRAN",
    "ROTA", "RM", "HOTPLUG", "RO",
)

#: The name the disk helper gives an array it puts together from drives
#: whose own record of it is lost.
BUILT_ARRAY = re.compile(r"^kss-raid([05])-\d+$")

#: lsblk device types listed as drives. Loop devices, optical drives and
#: device-mapper nodes are not drives; the latter appear under what they are
#: built on.
DISK_TYPES = frozenset(("disk", "loop"))

#: Devices of type "disk" that are not storage anyone formats: compressed RAM
#: swap, RAM disks, floppies, network block devices.
HIDDEN_PREFIXES = ("zram", "ram", "fd", "nbd")

#: Mount points the running system cannot do without. A volume mounted at one
#: of these - directly or through encryption, LVM or RAID stacked on it - is a
#: system volume: nothing destructive is offered on it.
#:
#: /var is listed part by part rather than as a whole tree. The parts the
#: system and its services live in are here; /var/mnt and /var/run/media are
#: not, because on an image-based system (Fedora Silverblue and its kin, where
#: /mnt is /var/mnt and /home is /var/home) that is where a user's own drives
#: are mounted.
SYSTEM_MOUNTS = frozenset((
    "/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home", "/etc",
    "/opt", "/root", "/srv", "/tmp", "/sysroot", "/nix", "/gnu",
    "/var/home", "/var/lib", "/var/log", "/var/cache", "/var/spool",
    "/var/tmp", "/var/opt", "/var/roothome", "/var/usrlocal", "/var/srv",
    # Where a live system keeps the stick it started from: the system runs
    # from a file on it, which no list of holders shows.
    "/run/initramfs/live", "/run/live/medium", "/lib/live/mount/medium",
    "/cdrom", "/run/kss/stick",
))
SYSTEM_PREFIXES = ("/boot/", "/usr/", "/etc/", "/sysroot/", "/nix/", "/gnu/",
                   "/var/lib/", "/var/log/", "/var/cache/")

SWAP = "[SWAP]"

#: MBR partition types that are containers for logical partitions.
EXTENDED_TYPES = frozenset(("0x5", "0xf", "0x85", "0x05", "0x0f"))

#: GPT type of an EFI System partition, and of Microsoft's reserved one.
GPT_EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
GPT_MSR = "e3c9e316-0b5c-4db8-817d-f92df00215ae"

# Kinds of drive, as told to the user.
NVME = "nvme"
SSD = "ssd"
HDD = "hdd"
USB = "usb"
CARD = "card"
IMAGE = "image"
RAID = "raid"
OTHER = "other"

KIND_TITLES = {
    NVME: "NVMe SSD",
    SSD: "SSD",
    HDD: "HDD",
    USB: "USB drive",
    CARD: "Memory card",
    IMAGE: "Disk image",
    RAID: "RAID array",
    OTHER: "Drive",
}


def is_system_mount(mountpoint):
    """Whether losing what is mounted here would take the system down."""
    if mountpoint == SWAP:
        return True
    return (mountpoint in SYSTEM_MOUNTS
            or any(mountpoint.startswith(p) for p in SYSTEM_PREFIXES))


@dataclass(frozen=True)
class Volume:
    """A partition, a filesystem written straight onto a disk, or what is
    stacked on one of those (an opened LUKS container, an LVM volume)."""

    name: str                    # sda1
    path: str                    # /dev/sda1
    disk: str                    # sda: the drive this is on
    kind: str                    # "part", "whole", or lsblk's type for inner
    size: int
    start: Optional[int] = None  # byte offset on the disk, partitions only
    number: Optional[int] = None
    fstype: str = ""
    fsver: str = ""
    label: str = ""
    uuid: str = ""
    partlabel: str = ""
    parttype: str = ""
    parttypename: str = ""
    partuuid: str = ""
    mountpoints: tuple = ()
    used: Optional[int] = None
    free: Optional[int] = None
    fs_size: Optional[int] = None
    cluster: Optional[int] = None  # bytes; mounted Windows filesystems only
    read_only: bool = False
    fstab_mount: str = ""        # where /etc/fstab mounts it, if it does
    inner: tuple = ()            # Volumes stacked on this one
    #: On the recovery stick: what a prepared change will do to it ("new",
    #: "formatted", "resized"...; see diskplan), "" for as it is.
    pending: str = ""

    # ---- what is mounted --------------------------------------------------

    def all_mountpoints(self):
        """Mount points of this volume and of everything stacked on it."""
        found = list(self.mountpoints)
        for child in self.inner:
            found.extend(child.all_mountpoints())
        return tuple(found)

    @property
    def mounted(self):
        return bool(self.all_mountpoints())

    @property
    def mountpoint(self):
        """The one to show: the first real directory, else nothing."""
        for mount in self.mountpoints:
            if mount != SWAP:
                return mount
        return ""

    @property
    def system_mounts(self):
        """The system mount points this volume carries, if any."""
        return tuple(m for m in self.all_mountpoints() if is_system_mount(m))

    @property
    def is_system(self):
        return bool(self.system_mounts)

    @property
    def is_swap(self):
        return self.fstype == "swap"

    @property
    def is_extended(self):
        return self.parttype.lower() in EXTENDED_TYPES

    @property
    def is_encrypted(self):
        return self.fstype == "crypto_LUKS"

    @property
    def has_filesystem(self):
        return bool(self.fstype) and not self.is_extended

    @property
    def title(self):
        """What to call it: its label, else the best description there is."""
        if self.label:
            return self.label
        if self.is_encrypted:
            return "Encrypted volume"
        if self.is_swap:
            return "Swap"
        if self.parttype.lower() == GPT_EFI:
            return "EFI System"
        if self.parttype.lower() == GPT_MSR:
            return "Microsoft reserved"
        if self.partlabel and self.partlabel.lower() not in ("primary",
                                                               "logical"):
            return self.partlabel
        if self.number is not None:
            return "Partition %d" % self.number
        return self.name


@dataclass(frozen=True)
class FreeSpace:
    """Unallocated room on a partitioned disk, MiB-aligned at both ends."""
    disk: str
    start: int
    size: int


@dataclass(frozen=True)
class Disk:
    name: str                    # sda
    path: str                    # /dev/sda
    size: int
    model: str = ""
    vendor: str = ""
    serial: str = ""
    wwn: str = ""
    revision: str = ""
    transport: str = ""          # sata, nvme, usb, mmc, ...
    rotational: bool = False
    removable: bool = False
    hotplug: bool = False
    read_only: bool = False
    table: str = ""              # gpt, dos, or "" for none
    table_uuid: str = ""
    logical_sector: int = 512
    physical_sector: int = 512
    discard: bool = False        # the device accepts TRIM
    volumes: tuple = ()
    free: tuple = ()
    fstype: str = ""             # a signature on the bare device, if any
    #: On the recovery stick: "image" when a prepared change writes an
    #: image onto it, so what it will hold is not known yet.
    pending: str = ""

    @property
    def kind(self):
        """What sort of drive this is, from what the kernel reports.

        The transport decides first: a USB device reports whatever its bridge
        chip feels like for "rotational" - most memory sticks claim to spin -
        so that flag is only believed on a link that carries it faithfully.
        """
        if self.name.startswith("loop"):
            return IMAGE
        if self.name.startswith("md") or self.name.startswith("dm-") \
                and self.model.startswith("RAID "):
            return RAID
        if self.transport == "nvme" or self.name.startswith("nvme"):
            return NVME
        if self.transport == "mmc" or self.name.startswith("mmcblk"):
            return CARD
        if self.transport == "usb":
            return USB
        if self.transport in ("sata", "ata", "sas", "scsi", "ide", "virtio",
                              ""):
            return HDD if self.rotational else SSD
        return OTHER

    @property
    def kind_title(self):
        return KIND_TITLES[self.kind]

    @property
    def is_solid_state(self):
        """Whether defragmenting this would be wear without benefit.

        A USB drive is counted as flash unless it reports spinning *and* is
        not a removable-media device: a memory stick's "rotational" flag
        cannot be trusted, an external hard disk's enclosure usually can.
        """
        kind = self.kind
        if kind in (NVME, SSD, CARD):
            return True
        if kind == USB:
            return self.removable or not self.rotational
        return False

    @property
    def has_media(self):
        """False for an empty card reader: a device with nothing in it."""
        return self.size > 0

    @property
    def title(self):
        text = " ".join(self.model.split())
        vendor = " ".join(self.vendor.split())
        # "ATA" is the SCSI layer's name for the bus, not a manufacturer.
        if vendor and vendor.upper() != "ATA" and \
                not text.lower().startswith(vendor.lower()):
            text = ("%s %s" % (vendor, text)).strip()
        return text or self.name

    @property
    def system_volumes(self):
        return tuple(v for v in self.volumes if v.is_system)

    @property
    def is_system(self):
        """Whether the running system lives on this drive, wholly or partly."""
        return bool(self.system_volumes)

    @property
    def mounted_volumes(self):
        return tuple(v for v in self.volumes if v.mounted)

    @property
    def holds_other_system(self):
        """Signs of an operating system that is not the running one.

        An EFI System or Microsoft reserved partition on a drive the running
        system does not use means another installation boots from here.
        """
        if self.is_system:
            return False
        return any(v.parttype.lower() in (GPT_EFI, GPT_MSR)
                   for v in self.volumes)

    @property
    def partitions(self):
        """The partitions that can hold something: no extended container."""
        return tuple(v for v in self.volumes
                     if v.kind == "part" and not v.is_extended)

    @property
    def unallocated(self):
        """Bytes no partition covers, counting only stretches worth using."""
        return sum(gap.size for gap in self.free)

    @property
    def written_as_image(self):
        """Whether an installer or live image was copied onto this drive.

        Such an image is an optical-disc filesystem laid over the whole
        device, with a partition table of its own inside it. The drive then
        shows the image's size, not its own, and stays that way until every
        signature is removed and a new table is written.
        """
        return (self.fstype == "iso9660"
                or any(v.fstype == "iso9660" for v in self.volumes))

    def volume(self, name):
        for volume in self.volumes:
            if volume.name == name:
                return volume
        return None


# ---- reading -----------------------------------------------------------------

class InventoryError(Exception):
    """lsblk could not be run or did not answer in a form we can read."""


def _run_lsblk(columns):
    return subprocess.run(
        ["lsblk", "--json", "--bytes", "--output", ",".join(columns)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        env=dict(os.environ, LC_ALL="C"))


def lsblk_json(run=None):
    """lsblk's view of every block device, as parsed JSON."""
    run = run or _run_lsblk
    if run is _run_lsblk and not shutil.which("lsblk"):
        raise InventoryError(
            "The 'lsblk' command was not found. It is part of util-linux, "
            "which every distribution installs.")
    last = "no output"
    for columns in (LSBLK_COLUMNS, LSBLK_FALLBACK):
        try:
            done = run(columns)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InventoryError("Could not run lsblk: %s" % exc)
        if done.returncode == 0:
            try:
                return json.loads(done.stdout.decode("utf-8", "replace"))
            except ValueError as exc:
                raise InventoryError("lsblk's answer could not be read: %s"
                                     % exc)
        last = done.stderr.decode("utf-8", "replace").strip() or last
    raise InventoryError("lsblk failed: %s" % last)


def _text(value):
    return "" if value is None else str(value).strip()


def _int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(value):
    """lsblk prints booleans as true/false in new releases, 0/1 in old."""
    if isinstance(value, bool):
        return value
    return _text(value).lower() in ("1", "true", "yes")


def _sys_int(name, attribute):
    try:
        with open(os.path.join(SYS_CLASS_BLOCK, name, attribute)) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _mountpoints(entry):
    found = entry.get("mountpoints")
    if found is None:
        found = [entry.get("mountpoint")]
    return tuple(m for m in found if m)


def read_fstab(path=None):
    """[(spec, mount point)] from /etc/fstab: what mounts itself at startup."""
    entries = []
    try:
        with open(path or FSTAB, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.split("#", 1)[0].split()
                if len(line) >= 2:
                    entries.append((line[0], line[1]))
    except OSError:
        pass
    return entries


def _fstab_mount(entry, fstab):
    """Where fstab mounts the device `entry` describes, or ""."""
    keys = {
        "UUID": _text(entry.get("uuid")),
        "LABEL": _text(entry.get("label")),
        "PARTUUID": _text(entry.get("partuuid")),
        "PARTLABEL": _text(entry.get("partlabel")),
    }
    path = _text(entry.get("path"))
    for spec, mount in fstab:
        tag, _, value = spec.partition("=")
        if value and tag in keys:
            if keys[tag] and keys[tag].lower() == value.strip('"').lower():
                return mount
        elif path and spec.startswith("/dev/"):
            try:
                if os.path.realpath(spec) == os.path.realpath(path):
                    return mount
            except OSError:
                continue
    return ""


#: The filesystems whose cluster size is worth showing: chosen when they are
#: made, from 512 bytes to 32 MiB. Linux filesystems all use 4 KiB.
CLUSTERED = frozenset(("exfat", "vfat", "ntfs"))

#: Replaced by the tests.
statvfs = os.statvfs


def cluster_size(fstype, mountpoints):
    """A mounted exFAT, FAT or NTFS volume's cluster size, else None.

    The kernel's exfat, vfat and ntfs3 drivers and ntfs-3g all report it as
    the block size of the mounted filesystem. An unmounted one would have to
    be read from the device, which only root can.
    """
    if fstype not in CLUSTERED:
        return None
    for mount in mountpoints:
        if mount.startswith("/"):
            try:
                return statvfs(mount).f_bsize or None
            except OSError:
                continue
    return None


def _volume(entry, disk, kind, fstab):
    name = _text(entry.get("kname")) or _text(entry.get("name"))
    start = _int(entry.get("start"))
    if start is None and kind == "part":
        start = _sys_int(name, "start")
    # Both lsblk and sysfs count a partition's start in 512-byte units,
    # whatever the drive's own sector size is.
    start_bytes = None if start is None else start * 512
    number = _int(entry.get("partn"))
    if number is None and kind == "part":
        number = _sys_int(name, "partition")

    inner = tuple(
        _volume(child, disk, _text(child.get("type")) or "inner", fstab)
        for child in entry.get("children") or ())

    return Volume(
        name=name,
        path=_text(entry.get("path")) or "/dev/%s" % name,
        disk=disk,
        kind=kind,
        size=_int(entry.get("size")) or 0,
        start=start_bytes,
        number=number,
        fstype=_text(entry.get("fstype")),
        fsver=_text(entry.get("fsver")),
        label=_text(entry.get("label")),
        uuid=_text(entry.get("uuid")),
        partlabel=_text(entry.get("partlabel")),
        parttype=_text(entry.get("parttype")),
        parttypename=_text(entry.get("parttypename")),
        partuuid=_text(entry.get("partuuid")),
        mountpoints=_mountpoints(entry),
        used=_int(entry.get("fsused")),
        free=_int(entry.get("fsavail")),
        fs_size=_int(entry.get("fssize")),
        cluster=cluster_size(_text(entry.get("fstype")),
                             _mountpoints(entry)),
        read_only=_flag(entry.get("ro")),
        fstab_mount=_fstab_mount(entry, fstab),
        inner=inner,
    )


def free_space(size, table, volumes, logical_sector=512):
    """The unallocated stretches of a partitioned disk, a MiB or more each.

    Both ends are aligned to a MiB, because that is where a partition made in
    them would be put: a gap that vanishes under alignment is not room for
    anything. A GPT keeps its backup header in the last 33 sectors, which are
    therefore not free however empty they look.
    """
    if table not in ("gpt", "dos") or size <= 0:
        return ()
    end_of_disk = size
    if table == "gpt":
        end_of_disk -= 33 * logical_sector
    spans = sorted((v.start, v.start + v.size) for v in volumes
                   if v.start is not None and not v.is_extended)
    gaps, cursor = [], MIB
    for start, end in spans + [(end_of_disk, end_of_disk)]:
        gap_start = -(-cursor // MIB) * MIB          # aligned up
        gap_end = (start // MIB) * MIB               # aligned down
        if gap_end - gap_start >= MIN_FREE:
            gaps.append((gap_start, gap_end - gap_start))
        cursor = max(cursor, end)
    return tuple(gaps)


def _disk(entry, fstab):
    name = _text(entry.get("kname")) or _text(entry.get("name"))
    size = _int(entry.get("size")) or 0
    logical = (_int(entry.get("log-sec"))
               or _sys_int(name, "queue/logical_block_size") or 512)
    physical = (_int(entry.get("phy-sec"))
                or _sys_int(name, "queue/physical_block_size") or logical)
    # Discard support is the kernel's to state. lsblk's DISC-MAX is the same
    # number as sysfs's discard_max_bytes; zero means the device - or the
    # bridge it sits behind - takes no TRIM.
    disc_max = _int(entry.get("disc-max"))
    if disc_max is None:
        disc_max = _sys_int(name, "queue/discard_max_bytes") or 0

    model = _text(entry.get("model"))
    if entry.get("array"):
        model = "RAID %s array" % entry["array"].replace("raid", "")
    if name.startswith("loop") and not model:
        # An image file opened as a drive: named after the file.
        try:
            with open(os.path.join(SYS_CLASS_BLOCK, name, "loop",
                                   "backing_file")) as handle:
                model = os.path.basename(handle.read().strip())
        except OSError:
            model = "Disk image"

    children = entry.get("children") or ()
    parts = [c for c in children if _text(c.get("type")) == "part"]
    volumes = [_volume(c, name, "part", fstab) for c in parts]

    table = _text(entry.get("pttype"))
    others = [c for c in children if _text(c.get("type")) != "part"]
    if not parts and (_text(entry.get("fstype")) or others):
        # A filesystem (or a LUKS container) written straight onto the device
        # with no partition table: the whole disk is the one volume.
        whole = dict(entry)
        whole["children"] = others
        whole.pop("start", None)
        volumes = [_volume(whole, name, "whole", fstab)]

    volumes = tuple(volumes)
    return Disk(
        name=name,
        path=_text(entry.get("path")) or "/dev/%s" % name,
        size=size,
        model=model,
        vendor=_text(entry.get("vendor")),
        serial=_text(entry.get("serial")),
        wwn=_text(entry.get("wwn")),
        revision=_text(entry.get("rev")),
        transport=_text(entry.get("tran")),
        rotational=_flag(entry.get("rota")),
        removable=_flag(entry.get("rm")),
        hotplug=_flag(entry.get("hotplug")),
        read_only=_flag(entry.get("ro")),
        table=table,
        table_uuid=_text(entry.get("ptuuid")),
        logical_sector=logical,
        physical_sector=physical,
        discard=disc_max > 0,
        volumes=volumes,
        free=tuple(FreeSpace(name, start, length) for start, length
                   in free_space(size, table, volumes, logical)),
        fstype=_text(entry.get("fstype")),
    )


def parse(data, fstab=None):
    """[Disk] from lsblk's JSON, in the order the kernel lists them."""
    fstab = read_fstab() if fstab is None else fstab
    found = []
    entries = list((data or {}).get("blockdevices") or ())
    # An assembled RAID array is listed under each of its members rather
    # than by itself; as far as what is on it goes, it is a drive.
    seen = set()
    for entry in list(entries):
        try:
            pending = list(entry.get("children") or ())
        except (AttributeError, TypeError):
            continue                     # described oddly: dealt with below
        while pending:
            child = pending.pop()
            if not isinstance(child, dict):
                continue
            nested = child.get("children")
            if isinstance(nested, (list, tuple)):
                pending.extend(nested)
            name = _text(child.get("kname")) or _text(child.get("name"))
            kind = _text(child.get("type"))
            # One put together here from drives whose own record of the
            # array is gone: a device-mapper node named for its level.
            built = BUILT_ARRAY.match(_text(child.get("name"))) \
                if kind == "dm" else None
            if (kind.startswith("raid") or built) and name not in seen:
                seen.add(name)
                array = dict(child)
                array["array"] = "raid" + built.group(1) if built else kind
                array["type"] = "disk"
                entries.append(array)
    for entry in entries:
        if _text(entry.get("type")) not in DISK_TYPES:
            continue
        name = _text(entry.get("kname")) or _text(entry.get("name"))
        if name.startswith(HIDDEN_PREFIXES):
            continue
        if _text(entry.get("type")) == "loop" and (
                not _int(entry.get("size"))
                or _text(entry.get("fstype")) in ("squashfs", "erofs")):
            # A loop device with nothing behind it, or one of the read-only
            # bundles software ships in: not a drive anyone manages.
            continue
        try:
            found.append(_disk(entry, fstab))
        except Exception:
            # One device lsblk describes oddly must not hide all the others.
            log.exception("Could not read block device %r", name)
    return found


#: On the recovery stick: what marks its system, the boot line that names
#: the stick, and where the stick is mounted.
RESCUE_MARKER = "/etc/kss-rescue"
CMDLINE = "/proc/cmdline"
STICK_MOUNT = "/run/kss/stick"


def stick_uuid(marker=None, cmdline=None):
    """On the recovery stick, the FAT ID of the stick it started from (the
    boot loader passes it on as kss.stick=); "" anywhere else."""
    if not os.path.exists(marker or RESCUE_MARKER):
        return ""
    try:
        with open(cmdline or CMDLINE) as handle:
            words = handle.read().split()
    except OSError:
        return ""
    for word in words:
        if word.startswith("kss.stick="):
            return word.split("=", 1)[1]
    return ""


def mark_stick(found, uuid):
    """The stick the system started from counts as the system's own drive
    even when it could not be mounted: nothing on it may be changed from
    the system running off it."""
    if not uuid:
        return found
    marked = []
    for disk in found:
        if any(v.uuid == uuid and STICK_MOUNT not in v.mountpoints
               for v in disk.volumes):
            volumes = tuple(
                replace(v, mountpoints=v.mountpoints + (STICK_MOUNT,))
                if v.uuid == uuid and STICK_MOUNT not in v.mountpoints
                else v for v in disk.volumes)
            disk = replace(disk, volumes=volumes)
        marked.append(disk)
    return marked


def inventory(run=None):
    """Every drive connected right now. Raises InventoryError."""
    return mark_stick(parse(lsblk_json(run)), stick_uuid())


# ---- identity ----------------------------------------------------------------

def _parent_disk(name):
    """The whole disk a partition belongs to; a disk is its own parent."""
    path = os.path.join(SYS_CLASS_BLOCK, name)
    if os.path.exists(os.path.join(path, "partition")):
        return os.path.basename(os.path.dirname(os.path.realpath(path)))
    return name


def identity(name):
    """What tells this device from another that later takes its name.

    Device names are handed out in the order things are plugged in: pull one
    memory stick and insert another and "sdd" is a different device. The
    kernel numbers every disk it attaches (diskseq, Linux 5.15) and never
    reuses a number, so that plus the size and, for a partition, where it
    starts is checked again by the privileged helper at the last moment. On a
    kernel without diskseq the size and offset still have to match.

    Only what both halves can read from sysfs goes in, so the helper compares
    like with like.
    """
    parent = _parent_disk(name)
    sectors = _sys_int(name, "size")
    return {
        "name": name,
        "disk": parent,
        "size": None if sectors is None else sectors * 512,
        "diskseq": _sys_int(parent, "diskseq"),
        "start": (_sys_int(name, "start") if parent != name else None),
    }


# ---- presentation ------------------------------------------------------------

def format_size(size, precision=None):
    """Bytes in decimal units, as drives are sold: an 8 TB disk reads 8.0 TB.

    The same convention as the main window's drive usage, so a volume does not
    change size between the monitor and the disk utility.
    """
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "kB", "MB", "GB", "TB", "PB"):
        if abs(value) < 1000.0 or unit == "PB":
            if unit == "B":
                return "%d B" % value
            if precision is None:
                digits = 0 if value >= 100 else 1
            else:
                digits = precision
            return "%.*f %s" % (digits, value, unit)
        value /= 1000.0
    return "%d B" % size
