"""Laptop batteries and UPS units from /sys/class/power_supply."""

import glob
import os

from .base import Device, Reading, SensorProvider

# Module level so tests can point it at a fixture tree.
POWER_SUPPLY_ROOT = "/sys/class/power_supply"


def _text(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except OSError:
        return ""


def _number(path):
    raw = _text(path)
    try:
        return float(raw)
    except ValueError:
        return None


def batteries():
    """[(device key, display name, sysfs path)] for each battery present.

    The machine's own batteries and UPS units, not a wireless mouse's: HID
    peripherals register as type Battery too, with scope Device. They come and
    go with the peripheral, and a 40% mouse beside the laptop battery would
    read as the machine's own charge.
    """
    found = []
    for path in sorted(glob.glob(os.path.join(POWER_SUPPLY_ROOT, "*"))):
        if _text(os.path.join(path, "type")) not in ("Battery", "UPS"):
            continue
        if _text(os.path.join(path, "scope")) == "Device":
            continue
        base = os.path.basename(path)
        vendor = _text(os.path.join(path, "manufacturer"))
        model = _text(os.path.join(path, "model_name"))
        name = " ".join(p for p in (vendor, model) if p) or "Battery (%s)" % base
        found.append(("battery:%s" % base, name, path))
    return found


class BatteryProvider(SensorProvider):
    name = "battery"

    def __init__(self):
        self._batteries = None

    def _found(self):
        if self._batteries is None:
            self._batteries = batteries()
        return self._batteries

    def devices(self):
        return [Device(key, name, order=70) for key, name, _ in self._found()]

    def read(self, ctx):
        out = []
        for key, _name, path in self._found():
            charge = _number(os.path.join(path, "capacity"))
            if charge is not None:
                out.append(Reading(key, "Charge", "capacity", "Charge",
                                   charge, "utilization"))

            # Wear: how much of the original design capacity remains. Reported
            # as energy_* on most laptops and charge_* on the rest.
            for full, design in (("energy_full", "energy_full_design"),
                                 ("charge_full", "charge_full_design")):
                now = _number(os.path.join(path, full))
                origin = _number(os.path.join(path, design))
                if now is not None and origin:
                    out.append(Reading(key, "Charge", "health", "Health",
                                       (now / origin) * 100, "utilization"))
                    break

            volts = _number(os.path.join(path, "voltage_now"))
            if volts is not None:
                out.append(Reading(key, "Voltages", "voltage", "Voltage",
                                   volts / 1_000_000.0, "voltage"))

            watts = _number(os.path.join(path, "power_now"))
            if watts is None:
                amps = _number(os.path.join(path, "current_now"))
                if amps is not None and volts is not None:
                    watts = (amps / 1_000_000.0) * (volts / 1_000_000.0) * 1_000_000
            if watts is not None:
                out.append(Reading(key, "Powers", "power", "Power",
                                   watts / 1_000_000.0, "power"))

            # Reported in tenths of a degree when present at all.
            temp = _number(os.path.join(path, "temp"))
            if temp is not None:
                out.append(Reading(key, "Temperatures", "temp", "Battery",
                                   temp / 10.0, "temp"))
        return out
