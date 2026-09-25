"""Intel graphics: the i915 driver and its newer xe replacement (Arc)."""

from ..hardware import gpu_cards
from ..hwmon import hwmon_sensors
from .base import Device, EnergyMeter, Reading, SensorProvider

DRIVERS = ("i915", "xe")

#: xe's channel labels (xe_hwmon.c). One label names a place on the card, not a
#: kind of sensor: "pkg" is a temperature, a voltage and an energy counter at
#: once, which is why sensors are told apart by attribute, never by label.
PLACE_NAMES = {"pkg": "Package", "card": "Card", "vram": "VRAM",
               "mctrl": "Memory Controller", "pcie": "PCIe"}


def _place(sensor, fallback):
    """Display name for where on the card a sensor sits."""
    if not sensor.labelled:
        return fallback
    label = sensor.label
    if label.startswith("vram_ch_"):
        return "VRAM Channel %s" % label[len("vram_ch_"):]
    return PLACE_NAMES.get(label, label.replace("_", " "))


def _numbered(sensors, fallback):
    """Names for unlabelled sensors: "GPU" alone, "GPU 1", "GPU 2" together."""
    bare = [s for s in sensors if not s.labelled]
    names = {}
    for sensor in sensors:
        if sensor.labelled:
            names[sensor.raw] = _place(sensor, fallback)
        elif len(bare) > 1:
            names[sensor.raw] = "%s %d" % (fallback, bare.index(sensor) + 1)
        else:
            names[sensor.raw] = fallback
    return names


class IntelGpuProvider(SensorProvider):
    name = "intel-gpu"
    chips = frozenset({"i915", "xe"})

    def __init__(self):
        self._cards = None
        self._energy = EnergyMeter()

    def _found(self):
        if self._cards is None:
            self._cards = [c for c in gpu_cards() if c.driver in DRIVERS]
        return self._cards

    def devices(self):
        return [Device(card.key, card.name, order=30) for card in self._found()]

    def read(self, ctx):
        out = []
        for card in self._found():
            hwmon = card.hwmon_dir()
            if hwmon:
                out.extend(self._hwmon(card, hwmon_sensors(hwmon)))
        return out

    def _hwmon(self, card, sensors):
        by_kind = {}
        for sensor in sensors:
            by_kind.setdefault(sensor.kind, []).append(sensor)

        out = []
        for kind, group, fallback, reading_kind in (
                ("temp", "Temperatures", "GPU", "temp"),
                ("in", "Voltages", "GPU", "voltage"),
                ("fan", "Fans", "Fan", "fan")):
            members = by_kind.get(kind, [])
            names = _numbered(members, fallback)
            for sensor in members:
                value = sensor.value("input")
                if value is not None:
                    out.append(Reading(card.key, group, sensor.raw,
                                       names[sensor.raw], value, reading_kind))

        out.extend(self._power(card, by_kind))
        return out

    def _power(self, card, by_kind):
        """Watts, from power*_input where published and energy otherwise.

        Neither i915 nor xe publishes power*_input - only limits, and an
        energy counter in energy*_input. The counter is what the driver
        measures, so power is its rate, and the first tick shows nothing.
        """
        out = []
        powers = by_kind.get("power", [])
        names = _numbered(powers, "Power")
        taken = set()
        for sensor in powers:
            value = sensor.value("input", "average")
            if value is not None:
                out.append(Reading(card.key, "Powers", sensor.raw,
                                   names[sensor.raw], value, "power"))
                taken.add(names[sensor.raw])

        energies = by_kind.get("energy", [])
        names = _numbered(energies, "Power")
        for sensor in energies:
            if names[sensor.raw] in taken:
                continue                 # the driver said it directly
            joules = sensor.value("input")
            if joules is None:
                continue
            watts = self._energy.rate((card.key, sensor.raw), joules)
            if watts is not None:
                out.append(Reading(card.key, "Powers", sensor.raw,
                                   names[sensor.raw], watts, "power"))
        return out
