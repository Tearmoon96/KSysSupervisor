"""Storage drives: NVMe controllers and, via drivetemp, SATA/SAS disks.

Reads each drive's own hwmon node rather than the global snapshot, so a
temperature can always be attributed to a specific disk and labelled with its
real model name.
"""

import glob
import os

import psutil

from ..hwmon import read_hwmon_dir
from .base import Advice, Device, Reading, SensorProvider, Step
from .kernel import modprobe_step

CHIPS = ("nvme", "drivetemp")

# Module level so tests can point these at a fixture tree.
HWMON_ROOT = "/sys/class/hwmon"
BLOCK_ROOT = "/sys/block"
CLASS_BLOCK_ROOT = "/sys/class/block"


def _disks_under(name, seen=None):
    """The whole disks a block device lives on: itself, or its parents.

    A partition's parent is the directory it sits in (sda/sda1); a device
    mapper or md device - LUKS, LVM, RAID - names what it is built on under
    slaves/, which may be partitions or further mappers in turn.
    """
    seen = set() if seen is None else seen
    if name in seen:
        return set()
    seen.add(name)
    path = os.path.join(CLASS_BLOCK_ROOT, name)
    slaves = glob.glob(os.path.join(path, "slaves", "*"))
    if slaves:
        found = set()
        for slave in slaves:
            found |= _disks_under(os.path.basename(slave), seen)
        return found
    if os.path.exists(os.path.join(path, "partition")):
        return {os.path.basename(os.path.dirname(os.path.realpath(path)))}
    return {name} if os.path.exists(path) else set()


def volumes_by_disk():
    """{disk name: [(mount point, used bytes, total bytes)]}, mounted only.

    Only what is mounted counts, since an unmounted partition has no "used"
    anyone can read. A filesystem is listed once however many times it is
    mounted - btrfs subvolumes put one filesystem at / and /home - under the
    first mount point, and one spread over several disks is listed on none:
    which of them is full has no answer.
    """
    volumes, seen = {}, set()
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError:
        return volumes
    for part in partitions:
        if not part.device.startswith("/dev/"):
            continue
        name = os.path.basename(os.path.realpath(part.device))
        if name in seen:
            continue
        seen.add(name)
        disks = _disks_under(name)
        if len(disks) != 1:
            continue
        try:
            du = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        volumes.setdefault(next(iter(disks)), []).append(
            (part.mountpoint, du.used, du.total))
    return volumes


def volume_label(mount):
    """What a volume is called on its ring: its mount point's last part.

    "/" is the system volume, and says so; /mnt/games is "games".
    """
    if mount == "/":
        return "System"
    return os.path.basename(mount.rstrip("/")) or mount


def _text(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except OSError:
        return ""


def _block_and_model(device_dir):
    """(block device name, model) for a drive's sysfs device directory."""
    model = _text(os.path.join(device_dir, "model"))

    # SATA/SAS present the block device one level down.
    for path in sorted(glob.glob(os.path.join(device_dir, "block", "*"))):
        return os.path.basename(path), model

    # NVMe controllers hold their namespaces directly (nvme0 -> nvme0n1).
    for path in sorted(glob.glob(os.path.join(device_dir, "nvme*n*"))):
        if os.path.isdir(path):
            return os.path.basename(path), model

    return "", model


def drives():
    """[(device key, display name, hwmon path)] for every drive with a sensor."""
    found = []
    for hwmon in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
        if _text(os.path.join(hwmon, "name")) not in CHIPS:
            continue

        device_dir = os.path.realpath(os.path.join(hwmon, "device"))
        block, model = _block_and_model(device_dir)
        if not block:
            # Still worth showing; fall back to the hwmon node's identity.
            block = os.path.basename(hwmon)

        found.append(("disk:%s" % block, model or block, hwmon, block))

    # Two identical drives produce two identically named categories with no way
    # to tell which is which, so name those by their block device as well.
    names = [name for _, name, _, _ in found]
    return [(key, "%s (%s)" % (name, block) if names.count(name) > 1 else name,
             hwmon)
            for key, name, hwmon, block in found]


def _block_of(key):
    """The block device a drive's key names: disk:sda -> sda."""
    return key.split(":", 1)[1] if ":" in key else key


class StorageProvider(SensorProvider):
    name = "storage"
    # Claimed so the mainboard provider does not also report these as
    # anonymous "NVME Composite" rows.
    chips = frozenset(CHIPS)

    def __init__(self):
        self._drives = None

    def _found(self):
        if self._drives is None:
            self._drives = drives()
        return self._drives

    def devices(self):
        return [Device(key, name, order=40) for key, name, _ in self._found()]

    def read(self, ctx):
        out = []
        for key, _name, hwmon in self._found():
            for label, attrs in ctx.sensors(read_hwmon_dir(hwmon)):
                value = ctx.first(attrs, 'input')
                if value is None:
                    continue
                out.append(Reading(key, "Temperatures", label,
                                   label.replace('_', ' ').title(), value, "temp"))
        # A reading per volume, keyed by where it is mounted. Decimal
        # gigabytes, as drives are sold: an 8 TB disk reads 8.0 TB.
        volumes = volumes_by_disk()
        for key, _name, _hwmon in self._found():
            for mount, used, total in volumes.get(_block_of(key), ()):
                if total:
                    out.append(Reading(key, "Utilization", "vol:%s" % mount,
                                       volume_label(mount), used / 1e9,
                                       "memory_gb", total=total / 1e9))
        return out

    @staticmethod
    def _ata_disks():
        """sd* devices on a real ATA/SAS link.

        USB sticks and card readers appear as sd* too, but drivetemp cannot
        read them, so counting those would offer a tip that never helps.
        """
        found = []
        for path in sorted(glob.glob(os.path.join(BLOCK_ROOT, "sd*"))):
            target = os.path.realpath(os.path.join(path, "device"))
            if "/usb" in target:
                continue
            found.append(os.path.basename(path))
        return found

    def advice(self):
        """SATA disks stay invisible until the drivetemp module is loaded."""
        if not self._ata_disks():
            return []
        if any(_text(os.path.join(h, "name")) == "drivetemp"
               for h in glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
            return []
        return [Advice(
            key="drivetemp",
            title="Enable SATA and SAS drive temperatures",
            problem="This machine has SATA or SAS drives, but the 'drivetemp' "
                    "module is not loaded, so none of them report a "
                    "temperature. Only NVMe drives are visible right now.",
            effect="Adds a category per SATA/SAS disk, named after its model, "
                   "with the temperature read from the drive's own SMART data.",
            steps=(
                modprobe_step("drivetemp",
                              "Loads the driver. Needs Linux 5.6 or newer."),
            ),
            persist=(
                Step("echo drivetemp | sudo tee /etc/modules-load.d/drivetemp.conf",
                     "Without this the module is gone after the next reboot "
                     "and the drives go quiet again. The file is read by "
                     "systemd-modules-load at every boot."),
            ),
            result="Restart KSysSupervisor. Each SATA disk appears as its own "
                   "category, e.g. 'CT1000MX500SSD1'.")]
