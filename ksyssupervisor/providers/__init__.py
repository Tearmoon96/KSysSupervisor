"""Sensor provider registry.

Takes one snapshot of the machine's sensors per tick, hands each provider the
hwmon chips it claims, and collects the resulting readings. Adding support for
new hardware means adding a provider here, not another branch in a dispatch
chain.
"""

import json
import shutil
from dataclasses import dataclass, field

from .. import log
from ..hwmon import read_hwmon_sysfs, run_cmd
from . import kernel
from .base import (GROUP_ORDER, Device, ReadContext, Reading,  # noqa: F401
                   SensorProvider)


@dataclass
class Sample:
    """Everything one tick produced."""

    readings: list = field(default_factory=list)
    backend: str = "none"
    issues: dict = field(default_factory=dict)   # key -> message, reported once
    advice: list = field(default_factory=list)   # hardware setup guidance


def chip_base(name):
    """'k10temp-pci-00c3' and 'k10temp-hwmon3' both -> 'k10temp'."""
    return name.split('-')[0]


def device_slots(chips):
    """Number chips that share a type.

    Two DIMMs or two NVMe drives otherwise produce identically labelled rows
    with no way to tell which device is which. Unique chips get slot 0.
    """
    groups = {}
    for name in sorted(chips):
        groups.setdefault(chip_base(name), []).append(name)

    slots = {}
    for names in groups.values():
        for i, name in enumerate(names, 1):
            slots[name] = i if len(names) > 1 else 0
    return slots


class ProviderRegistry:
    """Owns the providers and drives one read per tick."""

    def __init__(self, providers):
        self.providers = list(providers)
        self._devices = None

    @classmethod
    def default(cls):
        from .battery import BatteryProvider
        from .cpu import CpuProvider
        from .gpu_amd import AmdGpuProvider
        from .gpu_intel import IntelGpuProvider
        from .gpu_nvidia import NouveauProvider, NvidiaProvider
        from .mainboard import MainboardProvider
        from .memory import MemoryProvider
        from .storage import StorageProvider

        return cls([
            CpuProvider(),
            MemoryProvider(),
            AmdGpuProvider(),
            NvidiaProvider(),
            NouveauProvider(),
            IntelGpuProvider(),
            StorageProvider(),
            BatteryProvider(),
            MainboardProvider(),      # must stay last: it takes the leftovers
        ])

    # ---- devices -------------------------------------------------------

    def devices(self):
        """All discovered devices, keyed by Device.key. First declaration wins.

        Several GPU providers can contribute readings to the same card, so they
        all declare the same device; merging keeps one category for it.
        """
        if self._devices is None:
            merged = {}
            for provider in self.providers:
                try:
                    for device in provider.devices():
                        merged.setdefault(device.key, device)
                except Exception:
                    log.exception("Provider %s failed to enumerate devices",
                                  provider.name)
            self._devices = merged
        return self._devices

    def device(self, key):
        return self.devices().get(key)

    # ---- reading -------------------------------------------------------

    def snapshot(self):
        """(chips, backend, issues), preferring lm_sensors over raw sysfs."""
        issues = {}

        if shutil.which('sensors'):
            output = run_cmd(['sensors', '-j'], timeout=5.0)
            if output:
                try:
                    return json.loads(output), "lm_sensors", issues
                except json.JSONDecodeError as exc:
                    # Some lm_sensors builds emit invalid JSON for certain chips.
                    issues["sensors-json"] = (
                        "lm_sensors returned malformed JSON (%s). Falling back to "
                        "reading /sys/class/hwmon directly." % exc)
            else:
                issues["sensors-failed"] = (
                    "The 'sensors' command failed or timed out. Falling back to "
                    "reading /sys/class/hwmon directly.")

        chips = read_hwmon_sysfs()
        if chips:
            return chips, "sysfs", issues

        issues["no-sensors"] = (
            "No hardware sensors could be read, either through lm_sensors or "
            "/sys/class/hwmon. Temperature, fan, voltage and power readings are "
            "unavailable. Installing lm_sensors and running 'sudo sensors-detect' "
            "usually fixes this.")
        return {}, "none", issues

    def read_all(self):
        """One full pass over every provider. Never raises."""
        chips, backend, issues = self.snapshot()
        slots = device_slots(chips)

        claimed = {id(p): {} for p in self.providers}
        leftovers = {}
        for name, data in chips.items():
            base = chip_base(name)
            for provider in self.providers:
                if provider.claims(base):
                    claimed[id(provider)][name] = data
                    break
            else:
                leftovers[name] = data

        sample = Sample(backend=backend, issues=issues)
        # First: without a module tree none of the modprobe tips below
        # can succeed, so the user has to see that before trying them.
        sample.advice.extend(kernel.advice())
        for provider in self.providers:
            mine = dict(claimed[id(provider)])
            if provider.takes_leftovers:
                mine.update(leftovers)

            ctx = ReadContext(mine, slots, backend)
            try:
                sample.readings.extend(provider.read(ctx))
                sample.advice.extend(provider.advice() or [])
            except Exception:
                # One failing provider must not cost us every other reading.
                log.exception("Provider %s failed to read", provider.name)
                sample.issues.setdefault(
                    "provider-%s" % provider.name,
                    "The %s sensor provider failed; see the log for details."
                    % provider.name)
            sample.issues.update(ctx.issues)

        return sample
