"""CPU: utilization and clocks from psutil, temperatures and power from hwmon."""

import glob
import os
import re
import time

import psutil

from ..hardware import cpu_name
from .base import Advice, Device, EnergyMeter, Reading, SensorProvider, Step

DEVICE = "cpu"

# lm_sensors uses the vendor's register names; these read better in a UI.
TEMP_LABELS = {"Tctl": "Package", "Tdie": "Core"}
CCD_LABEL = re.compile(r"^Tccd(\d+)$")

# zenpower's SVI2 telemetry: power, voltage and current per rail.
SVI2_LABEL = re.compile(r"^SVI2_(?:[PC]_)?(Core|SoC)$")

#: Names for sensors a driver leaves unlabelled. k10temp only publishes
#: temp1_label on Zen; on the families before it (Phenom, FX, the A-series
#: APUs) the one temperature arrives as a bare temp1, and it is the package's.
UNLABELLED = {("k10temp", "temp1"): "Package",
              ("fam15h_power", "power1"): "Package"}

# What a sensor measures, from its attribute names - which the hwmon ABI fixes,
# unlike labels, which the driver makes up.
TEMP_ATTR = re.compile(r"^temp\d+_input$")
POWER_ATTR = re.compile(r"^power\d+_(input|average)$")
VOLT_ATTR = re.compile(r"^in\d+_input$")

RAPL_ROOT = "/sys/class/powercap"
RAPL_LABELS = {"core": "Cores", "uncore": "Uncore", "dram": "Memory",
               "psys": "Platform"}


class RaplReader:
    """CPU power from the powercap energy counters.

    The driver is called intel-rapl but AMD exposes the same interface, so this
    is the portable way to get package wattage. energy_uj is a monotonic
    microjoule counter, so power is its delta over elapsed time; the first tick
    only establishes a baseline and reports nothing.
    """

    def __init__(self):
        self._zones = None
        self._meter = EnergyMeter()
        self.permission_denied = False

    def zones(self):
        """(path, display name, wrap value) for each readable RAPL domain.

        A two-socket machine has package-0 and package-1, each with its own
        core and dram subzones; named alike, the second socket's readings
        replaced the first's. They are numbered only when there is more than
        one, so a desktop keeps its plain "Package".
        """
        if self._zones is not None:
            return self._zones

        found = []
        for path in sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:*"))):
            try:
                with open(os.path.join(path, "name"), 'r') as f:
                    raw = f.read().strip()
                with open(os.path.join(path, "max_energy_range_uj"), 'r') as f:
                    wrap = int(f.read().strip())
            except (OSError, ValueError):
                continue
            found.append((path, raw, wrap))

        # intel-rapl:<top>[:<sub>] - a subzone belongs to its top zone's package.
        packages = {}
        for path, raw, _wrap in found:
            top = os.path.basename(path).split(":")[1]
            if raw.startswith("package"):
                packages[top] = raw.partition("-")[2] or top
        several = len(packages) > 1

        self._zones = []
        for path, raw, wrap in found:
            top = os.path.basename(path).split(":")[1]
            if raw.startswith("package"):
                name = "Package %s" % packages[top] if several else "Package"
            else:
                name = RAPL_LABELS.get(raw, raw.replace('-', ' ').title())
                if several and top in packages:
                    name = "CPU %s %s" % (packages[top], name)
            self._zones.append((path, name, wrap))
        return self._zones

    def read(self):
        """Yield (display name, watts) for every domain with a usable delta."""
        now = time.monotonic()
        out = []
        for path, name, wrap in self.zones():
            try:
                with open(os.path.join(path, "energy_uj"), 'r') as f:
                    energy = int(f.read().strip())
            except PermissionError:
                # energy_uj is root-only on current kernels (side-channel
                # hardening); surfaced as setup advice rather than an error.
                self.permission_denied = True
                continue
            except (OSError, ValueError):
                continue

            watts = self._meter.rate(path, energy / 1_000_000.0, now,
                                     wrap=(wrap + 1) / 1_000_000.0)
            if watts is not None:
                out.append((name, watts))
        return out


class CpuProvider(SensorProvider):
    name = "cpu"
    chips = frozenset({"k10temp", "k8temp", "zenpower", "zenpower3",
                       "coretemp", "fam15h_power"})

    def __init__(self):
        self.rapl = RaplReader()

    def devices(self):
        return [Device(DEVICE, cpu_name(), order=10)]

    def read(self, ctx):
        return self._from_psutil(ctx) + self._from_hwmon(ctx) + self._from_rapl(ctx)

    def _from_rapl(self, ctx):
        return [Reading(DEVICE, "Powers", "rapl_%s" % name, name, watts, "power")
                for name, watts in self.rapl.read()]

    def advice(self):
        if not self.rapl.permission_denied:
            return []
        return [Advice(
            key="rapl-permission",
            title="Enable CPU package power readings",
            problem="The RAPL energy counters exist for this CPU but "
                    "/sys/class/powercap/*/energy_uj is readable only by root. "
                    "That is deliberate: fine-grained power readings let one "
                    "program infer what another is computing (the PLATYPUS "
                    "attack), so the kernel stopped exposing them. Opening "
                    "them up undoes that for every user on this machine - "
                    "usually fine on a single-user desktop, not on a shared "
                    "one.",
            effect="Adds live CPU package wattage (and per-domain Cores, "
                   "Uncore and Memory power where the CPU exposes them) under "
                   "the processor's Powers group.",
            steps=(
                Step("echo 'SUBSYSTEM==\"powercap\", ACTION==\"add\", "
                     "RUN+=\"/bin/chmod -R a+r /sys/devices/virtual/powercap\"' "
                     "| sudo tee /etc/udev/rules.d/99-powercap.rules",
                     "Grants read access to the energy counters at boot."),
                Step("sudo udevadm control --reload && sudo udevadm trigger "
                     "--action=add --subsystem-match=powercap",
                     "Applies the rule now, without rebooting. --action=add "
                     "matters: a plain trigger sends 'change', which the "
                     "rule above does not match."),
            ),
            persist_note="Nothing further to do - the rule file in "
                         "/etc/udev/rules.d is read on every boot, so this "
                         "survives reboots and kernel updates. Deleting that "
                         "file undoes it.",
            result="Restart KSysSupervisor. A 'Package' row in watts appears under "
                   "the CPU's Powers group within a second or two.")]

    def _from_psutil(self, ctx):
        out = []
        try:
            out.append(Reading(DEVICE, "Utilization", "usage_total", "Processor",
                               psutil.cpu_percent(interval=None), "utilization"))
        except Exception:
            ctx.issue("cpu-usage", "Could not read CPU utilization from psutil.")

        try:
            freqs = psutil.cpu_freq(percpu=True)
        except Exception:
            freqs = None
            ctx.issue("cpu-freq", "Could not read CPU clock speeds from psutil.")

        for i, freq in enumerate(freqs or []):
            if freq is not None and freq.current:
                out.append(Reading(DEVICE, "Clocks", "clock_%d" % i,
                                   "Core #%d" % i, freq.current, "clock"))
        return out

    def _from_hwmon(self, ctx):
        """Temperatures, power and rail voltages from the CPU's hwmon chips.

        Classified by attribute rather than by label: older k10temp families
        publish no label at all, and a test on label spelling ("starts with
        T") silently dropped their only temperature.

        A chip that has a twin - one coretemp or k10temp per socket - gets
        its socket's number in the key and the label. Both sockets report
        "Core 0" and "Tctl", and without it the second replaced the first.
        """
        out = []
        for chip, data in ctx.chips.items():
            base = chip.split('-')[0]
            slot = ctx.slots.get(chip, 0)
            for label, attrs in ctx.sensors(data):
                names = list(attrs)
                if any(TEMP_ATTR.match(n) for n in names):
                    group, kind = "Temperatures", "temp"
                    value = ctx.first(attrs, 'input')
                    nice = self._temp_label(base, label, slot)
                elif any(POWER_ATTR.match(n) for n in names):
                    group, kind = "Powers", "power"
                    value = ctx.first(attrs, 'input', 'average')
                    nice = self._rail_label(base, label)
                    if label == "PPT":
                        nice = "Package"
                elif any(VOLT_ATTR.match(n) for n in names):
                    group, kind = "Voltages", "voltage"
                    value = ctx.first(attrs, 'input')
                    nice = self._rail_label(base, label)
                else:
                    continue
                if value is None:
                    continue

                key = label.replace(' ', '_')
                if slot:
                    key = "cpu%d_%s" % (slot, key)
                    nice = "CPU %d %s" % (slot, nice)
                out.append(Reading(DEVICE, group, key, nice, value, kind))
        return out

    @staticmethod
    def _temp_label(base, label, slot):
        if (base, label) in UNLABELLED:
            return UNLABELLED[(base, label)]
        ccd = CCD_LABEL.match(label)
        if ccd:
            return "Core (CCD%s)" % ccd.group(1)
        if label in TEMP_LABELS:
            return TEMP_LABELS[label]
        if label.startswith("Package id"):
            # Per socket, the id is the socket number that "CPU n" already
            # gives, and "CPU 2 Package 1" would read as two different CPUs.
            return "Package" if slot else label.replace("Package id", "Package")
        if re.match(r"^temp\d+$", label):
            return "Temp %s" % label[4:]
        return label

    @staticmethod
    def _rail_label(base, label):
        if (base, label) in UNLABELLED:
            return UNLABELLED[(base, label)]
        svi2 = SVI2_LABEL.match(label)
        if svi2:
            return svi2.group(1)
        return label
