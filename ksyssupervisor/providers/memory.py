"""System memory: usage from psutil, DIMM temperatures from SPD sensors."""

import glob
import os
import re

import psutil

from .base import Advice, Device, Reading, SensorProvider, Step
from .kernel import modprobe_step

DEVICE = "ram"

STANDARD_SIZES = [2, 3, 4, 6, 8, 12, 16, 20, 24, 32, 36, 40, 48, 64, 72, 80,
                  96, 128, 160, 192, 256, 384, 512, 768, 1024, 1536, 2048]

#: How far short of a standard size the RAM may fall and still be called that
#: size: firmware reservations and small graphics carve-outs. Tight enough that
#: a 10 GB virtual machine is not rounded up to 12.
RESERVED_FRACTION = 0.15

# Module level so tests can point these at a fixture tree.
MEMMAP_ROOT = "/sys/firmware/memmap"
#: The udev database entry for the DMI tables. systemd's dmi_memory_id reads
#: the memory devices (SMBIOS type 17) as root at boot and records them here,
#: world-readable - the raw tables under /sys/firmware/dmi are root-only.
UDEV_DMI = "/run/udev/data/+dmi:id"

_DMI_FIELD = re.compile(r"^E:MEMORY_DEVICE_(\d+)_(\w+)=(.*)$")
_DMI_COUNT = re.compile(r"^E:MEMORY_ARRAY_NUM_DEVICES=(\d+)$")
# An SPD sensor's name ends in its SMBus address: spd5118-i2c-11-51.
_SPD_ADDRESS = re.compile(r"-i2c-(\d+)-([0-9a-fA-F]+)$")

#: How many slots the board has, once read: 0 when it is not known.
_slot_count = 0


def memory_slots(path=None):
    """(slot count, [filled slot numbers]) from the firmware, 1-based.

    Numbered in the firmware's own order, which is the board's: channel A's
    slots, then channel B's. (0, []) when udev has not recorded them.
    """
    fields, count = {}, 0
    try:
        with open(path or UDEV_DMI, "r") as f:
            for line in f:
                line = line.strip()
                match = _DMI_FIELD.match(line)
                if match:
                    fields.setdefault(int(match.group(1)), {})[
                        match.group(2)] = match.group(3)
                    continue
                match = _DMI_COUNT.match(line)
                if match:
                    count = int(match.group(1))
    except OSError:
        return 0, []
    count = count or len(fields)
    filled = []
    for index in sorted(fields):
        entry = fields[index]
        try:
            size = int(entry.get("SIZE", "0"))
        except ValueError:
            size = 0
        if size > 0 and entry.get("PRESENT", "1") != "0":
            filled.append(index + 1)
    return count, filled


def fill_rank(slot, count=None):
    """Where a slot comes in the order a board is filled.

    Boards with two slots per channel are populated from the second slot of
    each channel first - A2 and B2, the ones the manuals mark for two sticks
    - so on four slots the order is 2, 4, 1, 3. A board with one slot per
    channel, or one whose layout is unknown, fills in plain order.
    """
    count = _slot_count if count is None else count
    if count >= 4 and count % 2 == 0:
        return (slot % 2, slot)
    return (0, slot)


def _spd_order(chip):
    match = _SPD_ADDRESS.search(chip)
    if not match:
        return (1, chip)
    return (0, int(match.group(1)), int(match.group(2), 16))


def firmware_ram_bytes(root=None):
    """RAM the firmware handed the kernel, from its own memory map, or None.

    Closer to what is fitted than psutil's total, which has already lost the
    kernel's own reservations (its image, crashkernel): 31.6 GiB rather than
    30.9 on a 32 GB machine. Memory the firmware keeps for itself - a graphics
    carve-out - is "Reserved" there, alongside the PCI windows, and cannot be
    told apart from them, so a large carve-out still reads short.
    """
    total = 0
    for entry in glob.glob(os.path.join(root or MEMMAP_ROOT, "*")):
        try:
            with open(os.path.join(entry, "type")) as f:
                if f.read().strip() != "System RAM":
                    continue
            with open(os.path.join(entry, "start")) as f:
                start = int(f.read().strip(), 16)
            with open(os.path.join(entry, "end")) as f:
                end = int(f.read().strip(), 16)
        except (OSError, ValueError):
            continue
        total += end - start + 1
    return total or None


def installed_gb(raw_bytes=None):
    """Physical RAM, as the standard capacity the reported total falls short of.

    Firmware only ever keeps memory back, so the answer is the smallest
    standard size at or above the total rather than the nearest one: nearest
    showed a 20 GB laptop (16 + 4) as 16. A total no standard size explains
    within RESERVED_FRACTION is shown as it is.
    """
    if raw_bytes is None:
        raw_bytes = firmware_ram_bytes() or psutil.virtual_memory().total
    raw = raw_bytes / (1024 ** 3)
    for size in STANDARD_SIZES:
        if size >= raw:
            return size if raw >= size * (1 - RESERVED_FRACTION) else round(raw)
    return round(raw)


class MemoryProvider(SensorProvider):
    name = "memory"
    # spd5118 is DDR5; jc42 is the DDR3/DDR4 SPD temperature sensor.
    chips = frozenset({"spd5118", "jc42"})

    def __init__(self):
        self._total = None
        self._slots = None
        self._saw_dimm_sensor = False

    def devices(self):
        return [Device(DEVICE, "RAM", order=20)]

    def total_gb(self):
        if self._total is None:
            self._total = installed_gb()
        return self._total

    def read(self, ctx):
        out = []
        try:
            mem = psutil.virtual_memory()
            out.append(Reading(DEVICE, "Utilization", "usage", "Memory Used",
                               mem.used / (1024 ** 3), "memory_gb",
                               total=self.total_gb()))
        except Exception:
            ctx.issue("ram-usage", "Could not read memory usage from psutil.")

        self._saw_dimm_sensor = bool(ctx.chips)
        names = self._slot_names(ctx)
        for chip, data in sorted(ctx.chips.items(),
                                 key=lambda item: _spd_order(item[0])):
            for label, attrs in ctx.sensors(data):
                value = ctx.first(attrs, 'input')
                if value is None:
                    continue
                out.append(Reading(DEVICE, "Temperatures", "%s_%s" % (chip, label),
                                   names[chip], value, "temp"))
        return out

    def _slot_names(self, ctx):
        """"DIMM n" per SPD sensor, n being the slot the stick is in.

        The firmware says which slots are filled and the SPD addresses run
        in slot order (0x50 is the first slot's, 0x51 the second's), so the
        sensors in address order are the filled slots in order - when the
        two counts agree. Where they do not, or the firmware has not said,
        the sticks are numbered as they come, as before.
        """
        global _slot_count
        if self._slots is None:
            self._slots = memory_slots()
            _slot_count = self._slots[0]
        chips = sorted(ctx.chips, key=_spd_order)
        _count, filled = self._slots
        if filled and len(filled) == len(chips):
            return {chip: "DIMM %d" % slot for chip, slot in zip(chips, filled)}
        names = {}
        for chip in chips:
            slot = ctx.slots.get(chip, 0)
            names[chip] = "DIMM %d" % slot if slot else "DIMM"
        return names

    def advice(self):
        """DIMM temperature needs the SPD sensor driver for that DDR generation."""
        if self._saw_dimm_sensor:
            return []
        return [Advice(
            key="spd-sensor",
            title="Enable DIMM temperature readings",
            problem="No memory temperature sensor is being reported. Which "
                    "driver provides it depends on the memory generation - "
                    "spd5118 for DDR5, jc42 for DDR3 and DDR4 - and neither "
                    "loads automatically.",
            effect="Adds a temperature row per memory module under RAM.",
            steps_title="Try the one matching your memory",
            steps=(
                modprobe_step("spd5118", "For DDR5."),
                modprobe_step("jc42", "For DDR3 and DDR4."),
            ),
            persist=(
                Step("echo spd5118 | sudo tee /etc/modules-load.d/spd.conf",
                     "Use whichever module worked. Without this the module is "
                     "gone after the next reboot and the readings disappear; "
                     "the file is read by systemd-modules-load at every boot."),
            ),
            result="Restart KSysSupervisor and check RAM > Temperatures. Plenty of "
                   "modules have no temperature sensor fitted at all, so this "
                   "can legitimately find nothing even with the right driver.")]
