"""UDisks2, the desktop's disk service: reading it and asking it for things.

UDisks is what the file manager mounts drives through, and it reads each
drive's SMART data as root on everyone's behalf. Going through it means two
things need no password of ours: health data, and mounting a volume exactly
where the desktop would have mounted it.

It is spoken to through `busctl` (part of systemd) and `udisksctl` rather than
a D-Bus library, so this stays standard-library Python that the tests can drive
with a fake runner. Without either command, or without the service, the
callers get UDisksError and say what is missing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from . import log

SERVICE = "org.freedesktop.UDisks2"
ROOT = "/org/freedesktop/UDisks2"

BLOCK = "org.freedesktop.UDisks2.Block"
DRIVE = "org.freedesktop.UDisks2.Drive"
ATA = "org.freedesktop.UDisks2.Drive.Ata"
NVME = "org.freedesktop.UDisks2.NVMe.Controller"

#: Generous: a drive that was asleep has to spin up before it answers.
CALL_TIMEOUT = 45


class UDisksError(Exception):
    """UDisks is not there, refused, or answered something unreadable."""


def _run(argv, timeout=CALL_TIMEOUT):
    done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, env=dict(os.environ, LC_ALL="C"))
    return (done.returncode, done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"))


def available():
    return shutil.which("busctl") is not None


def unwrap(value):
    """busctl's JSON with the D-Bus type wrappers taken off.

    Every variant arrives as {"type": ..., "data": ...}; what callers want is
    the data. Applied all the way down, so a dictionary of variants becomes a
    plain dictionary.
    """
    if isinstance(value, dict):
        if set(value) == {"type", "data"}:
            return unwrap(value["data"])
        return {key: unwrap(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unwrap(item) for item in value]
    return value


def path_text(value):
    """A D-Bus byte-array path ("ay", NUL-terminated) as a string."""
    if isinstance(value, str):
        return value
    try:
        return bytes(b for b in value if b).decode("utf-8", "replace")
    except (TypeError, ValueError):
        return ""


def call(path, interface, method, signature="", *args, run=None,
         timeout=CALL_TIMEOUT):
    """One method call on the system bus; returns the unwrapped reply."""
    run = run or _run
    if run is _run and not available():
        raise UDisksError("The 'busctl' command (part of systemd) was not "
                          "found, so the disk service cannot be asked.")
    argv = ["busctl", "--system", "--json=short",
            "--timeout=%d" % timeout, "call", SERVICE, path, interface, method]
    if signature:
        argv.append(signature)
        argv.extend(str(a) for a in args)
    try:
        code, out, err = run(argv, timeout + 5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UDisksError("Could not reach the disk service: %s" % exc)
    if code != 0:
        raise UDisksError(_clean(err) or "the disk service refused")
    out = out.strip()
    if not out:
        return None
    try:
        reply = json.loads(out)
    except ValueError as exc:
        raise UDisksError("Unreadable answer from the disk service: %s" % exc)
    data = unwrap(reply)
    # A reply is a list of return values; nearly every method has one.
    if isinstance(data, list) and len(data) == 1:
        return data[0]
    return data


def _clean(text):
    """The human part of an error: what follows the last D-Bus error name."""
    text = " ".join(text.split())
    for marker in ("GDBus.Error:", "Call failed:"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
    if text.startswith("org.freedesktop.") and ": " in text:
        text = text.split(": ", 1)[1]
    return text


def objects(run=None):
    """{object path: {interface: {property: value}}} for everything UDisks
    knows, in one call."""
    found = call(ROOT, "org.freedesktop.DBus.ObjectManager",
                 "GetManagedObjects", run=run)
    return found if isinstance(found, dict) else {}


def drive_paths(managed):
    """{block device name: drive object path}, whole disks and partitions.

    The Block interface names the drive each block device belongs to, which is
    the only dependable link between "/dev/sda" and the object carrying its
    SMART data - the drive's object path is built from model and serial.
    """
    found = {}
    for interfaces in managed.values():
        block = interfaces.get(BLOCK)
        if not block:
            continue
        device = path_text(block.get("Device"))
        drive = block.get("Drive")
        if device and isinstance(drive, str) and drive != "/":
            found[os.path.basename(device)] = drive
    return found


# ---- things asked of it ------------------------------------------------------

def smart_update(drive_path, interfaces, run=None):
    """Have UDisks read the drive's SMART data again. Never wakes a disk.

    Best effort: a drive that is asleep, or a desktop policy that wants a
    password for this, leaves the data UDisks already had.
    """
    interface = NVME if NVME in interfaces else ATA if ATA in interfaces else None
    if interface is None:
        return False
    try:
        call(drive_path, interface, "SmartUpdate", "a{sv}", 1, "nowakeup",
             "b", "true", run=run)
        return True
    except UDisksError as exc:
        log.debug("SMART refresh of %s declined: %s", drive_path, exc)
        return False


def smart_attributes(drive_path, interfaces, run=None):
    """The drive's attribute table: a list (ATA) or a dictionary (NVMe)."""
    interface = NVME if NVME in interfaces else ATA if ATA in interfaces else None
    if interface is None:
        return None
    try:
        return call(drive_path, interface, "SmartGetAttributes", "a{sv}", 0,
                    run=run)
    except UDisksError as exc:
        log.debug("No SMART attributes from %s: %s", drive_path, exc)
        return None


SELFTESTS = ("short", "extended")


def _smart_interface(interfaces):
    return NVME if NVME in interfaces else ATA if ATA in interfaces else None


def selftest_start(drive_path, interface, kind, run=None):
    """Start the drive's built-in self-test. The desktop asks for the
    administrator's password itself; the test then runs inside the drive."""
    if kind not in SELFTESTS:
        raise UDisksError("Unknown self-test.")
    if interface not in (ATA, NVME):
        raise UDisksError("This drive has no self-test.")
    call(drive_path, interface, "SmartSelftestStart", "sa{sv}", kind, 0,
         run=run, timeout=120)


def selftest_abort(drive_path, interface, run=None):
    if interface not in (ATA, NVME):
        raise UDisksError("This drive has no self-test.")
    call(drive_path, interface, "SmartSelftestAbort", "a{sv}", 0, run=run)


def _udisksctl(args, run=None):
    run = run or _run
    if run is _run and not shutil.which("udisksctl"):
        raise UDisksError(
            "The 'udisksctl' command was not found. Install UDisks2 "
            "(package 'udisks2') to mount and eject from here.")
    try:
        code, out, err = run(["udisksctl"] + list(args), 120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UDisksError("Could not run udisksctl: %s" % exc)
    if code != 0:
        raise UDisksError(_clean(err or out) or "udisksctl failed")
    return out.strip()


# ---- on the recovery stick ----------------------------------------------------
#
# The stick's system has no UDisks and no D-Bus, and the app runs as root
# there: mounting, unmounting and ejecting are done with the plain tools.

#: Where the stick mounts what the user asks to have mounted.
STICK_MOUNTS = "/run/kss/mnt"


def on_stick():
    return os.environ.get("KSS_RESCUE") == "1" and os.geteuid() == 0


def _plain(argv, run=None):
    run = run or _run
    try:
        code, out, err = run(argv, 120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UDisksError("Could not run %s: %s" % (argv[0], exc))
    if code != 0:
        raise UDisksError(_clean(err or out) or "%s failed" % argv[0])
    return out


def mount_points(device, mounts="/proc/self/mounts"):
    """Where a device is mounted, from the kernel's own list."""
    found = []
    real = os.path.realpath(device)
    try:
        with open(mounts) as handle:
            for line in handle:
                source, point = line.split()[:2]
                if source.startswith("/dev/") and \
                        os.path.realpath(source) == real:
                    found.append(point.replace("\\040", " "))
    except OSError:
        pass
    return found


def _stick_mount(device, run=None):
    label = ""
    try:
        label = _plain(["blkid", "-o", "value", "-s", "LABEL", device],
                       run).strip()
    except UDisksError:
        pass
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or \
        os.path.basename(device)
    point = os.path.join(STICK_MOUNTS, name)
    serial = 1
    while os.path.exists(point) and os.listdir(point):
        serial += 1
        point = os.path.join(STICK_MOUNTS, "%s-%d" % (name, serial))
    os.makedirs(point, exist_ok=True)
    try:
        _plain(["mount", device, point], run)
    except UDisksError:
        try:
            os.rmdir(point)
        except OSError:
            pass
        raise
    return point


def _stick_unmount(device, run=None):
    for point in mount_points(device)[::-1]:
        _plain(["umount", point], run)
        if point.startswith(STICK_MOUNTS + "/"):
            try:
                os.rmdir(point)
            except OSError:
                pass


def _stick_power_off(device, run=None):
    """Everything on it unmounted (the window does the volumes first), its
    cache written out, and the kernel told to let it go."""
    _stick_unmount(device, run)
    os.sync()
    name = os.path.basename(os.path.realpath(device))
    try:
        with open("/sys/block/%s/device/delete" % name, "w") as handle:
            handle.write("1\n")
    except OSError:
        pass


def mount(device, run=None):
    """Mount a volume where the desktop would; returns the mount point."""
    if on_stick():
        return _stick_mount(device, run)
    out = _udisksctl(["mount", "--block-device", device], run)
    # "Mounted /dev/sdd1 at /run/media/luca/NAME"
    marker = " at "
    if marker in out:
        return out.split(marker, 1)[1].strip().rstrip(".")
    return ""


def unmount(device, run=None):
    if on_stick():
        return _stick_unmount(device, run)
    _udisksctl(["unmount", "--block-device", device], run)


def power_off(device, run=None):
    """Spin down and detach a drive so it can be unplugged."""
    if on_stick():
        return _stick_power_off(device, run)
    _udisksctl(["power-off", "--block-device", device], run)
