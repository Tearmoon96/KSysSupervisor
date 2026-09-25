"""NVIDIA GPUs.

Two separate paths: nvidia-smi for the proprietary driver, and the nouveau
hwmon node for the open one, so a card works either way.
"""

import shutil

from ..hardware import gpu_cards
from ..hwmon import hwmon_sensors, nv_value, run_cmd
from .base import Device, Reading, SensorProvider

QUERY = ("pci.bus_id,name,temperature.gpu,utilization.gpu,memory.used,"
         "memory.total,power.draw,power.limit,clocks.current.graphics,"
         "clocks.current.memory,fan.speed")
FIELDS = 11


def normalize_bus_id(raw):
    """nvidia-smi prints 00000000:03:00.0; sysfs uses 0000:03:00.0."""
    parts = raw.strip().lower().split(':')
    if len(parts) == 3:
        return "%s:%s:%s" % (parts[0][-4:].rjust(4, '0'), parts[1], parts[2])
    return raw.strip().lower()


class NvidiaProvider(SensorProvider):
    """Every NVIDIA GPU reported by nvidia-smi, not just the first."""

    name = "nvidia-gpu"

    def __init__(self):
        self._names = None

    def _query(self):
        if not shutil.which("nvidia-smi"):
            return []
        out = run_cmd(["nvidia-smi", "--query-gpu=" + QUERY,
                       "--format=csv,noheader,nounits"], timeout=5.0)
        if not out or not out.strip():
            return []

        rows = []
        for line in out.strip().split('\n'):
            parts = [p.strip() for p in line.split(',')]
            if len(parts) == FIELDS:
                rows.append(parts)
        return rows

    def devices(self):
        if self._names is None:
            self._names = [(normalize_bus_id(row[0]), row[1]) for row in self._query()]
        return [Device("gpu:%s" % pci, name, order=30) for pci, name in self._names]

    def read(self, ctx):
        rows = self._query()
        if not rows:
            # No nvidia-smi at all is normal on most machines and stays silent;
            # nvidia-smi present but mute means something is actually wrong.
            if shutil.which("nvidia-smi"):
                ctx.issue("nvidia-smi",
                          "nvidia-smi is installed but returned no usable data. "
                          "NVIDIA GPU readings are unavailable - the driver may "
                          "not be loaded correctly.")
            return []

        out = []
        for row in rows:
            key = "gpu:%s" % normalize_bus_id(row[0])
            (temp, util, vram_used, vram_total, draw, limit,
             gfx, mem, fan) = (nv_value(p) for p in row[2:])

            if temp is not None:
                out.append(Reading(key, "Temperatures", "edge", "GPU", temp, "temp"))
            if util is not None:
                out.append(Reading(key, "Utilization", "usage_total", "GPU",
                                   util, "utilization"))
            if vram_used is not None and vram_total:
                used_gb, total_gb = vram_used / 1024, vram_total / 1024
                out.append(Reading(key, "VRAM", "vram", "VRAM", used_gb,
                                   "memory_gb", total=total_gb))
            if draw is not None:
                out.append(Reading(key, "Powers", "power", "Power", draw, "power"))
            if draw is not None and limit:
                out.append(Reading(key, "Utilization", "power_pct", "Power Limit",
                                   (draw / limit) * 100, "utilization"))
            if gfx is not None:
                out.append(Reading(key, "Clocks", "sclk", "Graphics", gfx, "clock"))
            if mem is not None:
                out.append(Reading(key, "Clocks", "mclk", "Memory", mem, "clock"))
            if fan is not None:
                out.append(Reading(key, "Fans", "fan", "Fan", fan, "fan"))
        return out


class NouveauProvider(SensorProvider):
    """NVIDIA cards on the open nouveau driver, which has no nvidia-smi."""

    name = "nouveau-gpu"
    chips = frozenset({"nouveau"})

    def __init__(self):
        self._cards = None

    def _found(self):
        if self._cards is None:
            # Every card bound to nouveau, whether or not nvidia-smi is
            # installed: a card cannot be on both drivers at once, so nothing
            # nvidia-smi reports is ever one of these. Skipping them whenever
            # the binary existed hid every nouveau card on a machine that had
            # the proprietary tools installed but not in use.
            self._cards = [c for c in gpu_cards() if c.driver == "nouveau"]
        return self._cards

    def devices(self):
        return [Device(card.key, card.name, order=30) for card in self._found()]

    #: hwmon sensor kind -> (group, row label, reading kind). Matched on the
    #: attribute, not the label: nouveau labels its core rail "GPU core",
    #: which no amount of looking for "in" or "volt" in a label would find.
    KINDS = {"temp": ("Temperatures", "GPU", "temp"),
             "fan": ("Fans", "Fan", "fan"),
             "power": ("Powers", "Power", "power"),
             "in": ("Voltages", "GPU", "voltage")}

    def read(self, ctx):
        out = []
        for card in self._found():
            hwmon = card.hwmon_dir()
            if not hwmon:
                continue
            for sensor in hwmon_sensors(hwmon):
                kind = self.KINDS.get(sensor.kind)
                value = sensor.value("input", "average")
                if kind is None or value is None:
                    continue
                group, label, reading_kind = kind
                out.append(Reading(card.key, group, sensor.raw, label,
                                   value, reading_kind))
        return out
