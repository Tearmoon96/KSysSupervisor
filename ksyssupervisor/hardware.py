"""Identifying the machine's CPU, GPU and mainboard for display.

Kept separate from the sensor providers because several providers contribute
readings to the same GPU category and all of them need the same display name.
"""

import os
import re
import glob

from .hwmon import run_cmd


# "AMD Ryzen 5 5600X 6-Core Processor", "... PRO 7995WX 96-Cores": the core
# count is a marketing suffix, in whatever number the part has, not only the 8
# and 16 once listed.
_CORE_SUFFIX = re.compile(r"\s*\b\d+-Cores?(?: Processor)?\b")


def cpu_name():
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("model name"):
                    name = line.split(":", 1)[1].strip()
                    name = _CORE_SUFFIX.sub("", name).strip()
                    name = name.replace("Processor", "").strip()
                    name = name.replace("(R)", "").replace("(TM)", "").strip()
                    name = name.replace("CPU", "").strip()
                    if " @ " in name:
                        name = name.split(" @ ")[0].strip()
                    return name
    except Exception:
        pass
    return "Processor"

class GpuCard:
    """One DRM card: how to identify it, name it and find its sensors."""

    def __init__(self, pci, driver, path, name):
        self.pci = pci          # "0000:03:00.0"
        self.driver = driver    # "amdgpu", "radeon", "i915", "xe", "nouveau"
        self.path = path        # /sys/class/drm/cardN/device
        self.name = name

    @property
    def key(self):
        return "gpu:%s" % self.pci

    def hwmon_dir(self):
        """The card's own hwmon node, so readings need no chip-name guessing."""
        found = sorted(glob.glob(os.path.join(self.path, "hwmon", "hwmon*")))
        return found[0] if found else None

    def __repr__(self):
        return "GpuCard(%s, %s, %r)" % (self.pci, self.driver, self.name)


def pci_name(pci_id):
    """Human-readable device name from lspci, or "" if unavailable."""
    short = pci_id[5:] if pci_id.startswith("0000:") else pci_id
    try:
        output = run_cmd(["lspci", "-s", short]) or ""
    except Exception:
        return ""
    if ":" not in output:
        return ""
    parts = output.split(":", 2)
    if len(parts) < 3:
        return ""
    name = parts[2].strip()
    name = name.replace("Advanced Micro Devices, Inc. [AMD/ATI]", "").strip()
    name = name.replace("Intel Corporation", "Intel").strip()
    if " (rev " in name:
        name = name.split(" (rev ")[0].strip()
    return name


def gpu_cards():
    """Every graphics card present, in PCI order.

    Enumerating DRM cards rather than picking the one with the most VRAM means
    a machine with both an integrated and a discrete GPU reports both.
    """
    cards = []
    for device in sorted(glob.glob('/sys/class/drm/card*/device')):
        card = os.path.basename(os.path.dirname(device))
        if '-' in card:
            continue                    # a connector (card1-DP-1), not the card

        pci = os.path.basename(os.path.realpath(device))
        driver = ""
        link = os.path.join(device, 'driver')
        if os.path.islink(link):
            driver = os.path.basename(os.path.realpath(link))

        name = pci_name(pci) or (driver.upper() if driver else "") or "Graphics Card"
        cards.append(GpuCard(pci, driver, device, name))
    return cards


def mobo_name():
    try:
        vendor = ""
        name = ""
        with open("/sys/class/dmi/id/board_vendor", "r") as f:
            vendor = f.read().strip()
        with open("/sys/class/dmi/id/board_name", "r") as f:
            name = f.read().strip()
        if vendor and name:
            return f"{vendor} {name}"
    except Exception:
        pass
    return "Motherboard / System"
