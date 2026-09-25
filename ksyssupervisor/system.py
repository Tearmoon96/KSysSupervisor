"""Platform and optional-dependency checks performed before the UI starts."""

import os
import sys
import glob
import shutil
import platform

from . import (EXIT_NO_DISPLAY, EXIT_UNSUPPORTED_PLATFORM, emergency_notify,
               log)


def has_nvidia_gpu():
    """True if an NVIDIA display controller is present on the PCI bus."""
    for dev_path in glob.glob('/sys/bus/pci/devices/*'):
        try:
            with open(os.path.join(dev_path, 'vendor'), 'r') as vf:
                vendor = vf.read().strip()
            with open(os.path.join(dev_path, 'class'), 'r') as cf:
                pci_class = cf.read().strip()
        except OSError:
            continue  # One unreadable device must not abort the whole scan
        if vendor == '0x10de' and pci_class.startswith('0x03'):
            return True
    return False


def check_dependencies():
    """Report optional system utilities that are missing.

    Returns (missing, recommendations). Nothing here is fatal and nothing
    blocks startup: the app degrades to reading /sys/class/hwmon directly.
    """
    missing = []
    recommendations = []

    if not shutil.which('sensors'):
        missing.append("lm_sensors ('sensors' command)")
        recommendations.append(
            "- Install 'lm_sensors' for full temperature, voltage and fan coverage.\n"
            "  Fedora:         sudo dnf install lm_sensors\n"
            "  Debian/Ubuntu:  sudo apt install lm-sensors\n"
            "  Arch:           sudo pacman -S lm_sensors\n"
            "  then:           sudo sensors-detect")

    if not shutil.which('lspci'):
        missing.append("pciutils ('lspci' command)")
        recommendations.append(
            "- Install 'pciutils' to accurately identify your hardware component names.")

    if has_nvidia_gpu() and not shutil.which('nvidia-smi'):
        missing.append("NVIDIA Drivers ('nvidia-smi' command)")
        recommendations.append(
            "- Install the proprietary NVIDIA drivers to read NVIDIA GPU temperatures,\n"
            "  utilization and power.")

    if missing:
        log.warning("Missing optional utilities: %s", ", ".join(missing))
    return missing, recommendations


def check_compatibility():
    """Verify the platform. Only genuinely fatal problems exit."""
    if not sys.platform.startswith('linux'):
        emergency_notify(
            "Unsupported operating system",
            "KSysSupervisor reads Linux sysfs interfaces and only runs on Linux.\n\n"
            "Detected: %s" % sys.platform)
        sys.exit(EXIT_UNSUPPORTED_PLATFORM)

    arch = platform.machine().lower()
    if arch not in ('x86_64', 'amd64'):
        # Not fatal: the psutil and sysfs paths are architecture independent.
        log.warning("Unsupported architecture '%s'; some readings may be unavailable.", arch)

    try:
        major = int(platform.release().split('.')[0])
    except (ValueError, IndexError):
        log.warning("Could not parse the kernel version %r", platform.release())
    else:
        if major < 4:
            emergency_notify(
                "Kernel too old",
                "KSysSupervisor needs Linux 4.0 or newer for the sysfs layout it reads.\n\n"
                "Detected kernel: %s" % platform.release())
            sys.exit(EXIT_UNSUPPORTED_PLATFORM)

    if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
        emergency_notify(
            "No graphical display",
            "KSysSupervisor needs a running X11 or Wayland session, but neither\n"
            "DISPLAY nor WAYLAND_DISPLAY is set.\n\n"
            "If you are connected over SSH, reconnect with 'ssh -X'.")
        sys.exit(EXIT_NO_DISPLAY)
