"""AMD GPUs.

Covers both the modern amdgpu driver and the older radeon driver used by
pre-GCN cards. Each card is read through its own sysfs and hwmon nodes, so a
machine with an integrated and a discrete Radeon reports both separately.
"""

import os

from ..hardware import gpu_cards
from .base import Device, Reading, SensorProvider

DRIVERS = ("amdgpu", "radeon")

TEMP_LABELS = {"edge": "GPU", "junction": "Hot Spot", "mem": "Memory"}


def _int_file(path):
    try:
        with open(path, 'r') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class AmdGpuProvider(SensorProvider):
    name = "amd-gpu"
    chips = frozenset({"amdgpu", "radeon"})

    def __init__(self):
        self._cards = None
        self._dpm_supported = {}     # pci -> bool

    def _found(self):
        if self._cards is None:
            self._cards = [c for c in gpu_cards() if c.driver in DRIVERS]
        return self._cards

    def devices(self):
        return [Device(card.key, card.name, order=30) for card in self._found()]

    # ---- per-card sysfs -------------------------------------------------

    def dpm_supported(self, card):
        """True when the card exposes pp_dpm_* at all (resolved once).

        Checked separately from the per-tick reading: the active-level '*'
        marker can be briefly absent while the driver switches levels, and
        falling back to the hwmon freq for that tick would record a bogus
        0 MHz minimum that never clears. The radeon driver has no pp_dpm_* at
        all, so it keeps using hwmon.
        """
        if card.pci not in self._dpm_supported:
            self._dpm_supported[card.pci] = os.path.exists(
                os.path.join(card.path, "pp_dpm_sclk"))
        return self._dpm_supported[card.pci]

    @staticmethod
    def dpm_clocks(card):
        """Current clocks in MHz from pp_dpm_sclk / pp_dpm_mclk.

        The amdgpu hwmon freq*_input values are a gated average that reads far
        below the true clock while the GPU idles (4 MHz for a card actually
        sitting at 500 MHz). pp_dpm_* marks the active level with a '*'.
        """
        clocks = {}
        for key, attr in (("sclk", "pp_dpm_sclk"), ("mclk", "pp_dpm_mclk")):
            try:
                with open(os.path.join(card.path, attr), 'r') as f:
                    content = f.read()
            except OSError:
                continue
            for line in content.splitlines():
                if not line.rstrip().endswith('*'):
                    continue
                for token in line.lower().split():
                    if token.endswith('mhz'):
                        try:
                            clocks[key] = float(token[:-3])
                        except ValueError:
                            pass
                        break
                break
        return clocks

    def _sysfs(self, card, ctx):
        out = []

        busy = _int_file(os.path.join(card.path, "gpu_busy_percent"))
        if busy is not None:
            out.append(Reading(card.key, "Utilization", "usage_total", "GPU",
                               float(busy), "utilization"))

        used = _int_file(os.path.join(card.path, "mem_info_vram_used"))
        total = _int_file(os.path.join(card.path, "mem_info_vram_total"))
        if used is not None and total:
            # One reading, not an amount and its percentage as well: the
            # share is used / total, which the total already gives.
            out.append(Reading(card.key, "VRAM", "vram", "VRAM",
                               used / (1024 ** 3), "memory_gb",
                               total=total / (1024 ** 3)))

        clocks = self.dpm_clocks(card)
        if 'sclk' in clocks:
            out.append(Reading(card.key, "Clocks", "sclk", "Graphics",
                               clocks['sclk'], "clock"))
        if 'mclk' in clocks:
            out.append(Reading(card.key, "Clocks", "mclk", "Memory",
                               clocks['mclk'], "clock"))
        return out

    # ---- per-card hwmon -------------------------------------------------

    def _hwmon(self, card, ctx):
        from ..hwmon import read_hwmon_dir

        hwmon = card.hwmon_dir()
        if not hwmon:
            return []

        out = []
        for label, attrs in ctx.sensors(read_hwmon_dir(hwmon)):
            if 'temp' in label or label in TEMP_LABELS:
                value = ctx.first(attrs, 'input')
                if value is None:
                    continue
                nice = TEMP_LABELS.get(
                    label,
                    "Temp %s" % label[-1] if label.startswith('temp')
                    else label.capitalize())
                out.append(Reading(card.key, "Temperatures", label, nice,
                                   value, "temp"))

            elif 'power' in label or 'PPT' in label:
                cap = ctx.first(attrs, 'cap')
                current = ctx.first(attrs, 'average', 'input')
                if current is not None:
                    out.append(Reading(card.key, "Powers", label, "Power",
                                       current, "power"))
                if current is not None and cap:
                    out.append(Reading(card.key, "Utilization", "%s_pct" % label,
                                       "Power Limit", (current / cap) * 100,
                                       "utilization"))

            elif 'fan' in label:
                value = ctx.first(attrs, 'input')
                if value is not None:
                    out.append(Reading(card.key, "Fans", label, "Fan", value, "fan"))

            elif 'vdd' in label or label.startswith('in'):
                value = ctx.first(attrs, 'input')
                if value is not None:
                    out.append(Reading(card.key, "Voltages", label,
                                       "GPU" if 'gfx' in label else label,
                                       value, "voltage"))

            elif label in ('sclk', 'mclk'):
                if self.dpm_supported(card):
                    continue          # pp_dpm_* is the authoritative source
                value = ctx.first(attrs, 'input')
                if value is not None:
                    out.append(Reading(
                        card.key, "Clocks", label,
                        "Graphics" if label == 'sclk' else "Memory",
                        value / 1_000_000, "clock"))
        return out

    def read(self, ctx):
        out = []
        for card in self._found():
            out.extend(self._sysfs(card, ctx))
            out.extend(self._hwmon(card, ctx))
        return out
