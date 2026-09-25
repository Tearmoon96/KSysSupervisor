"""Reading hardware sensors from the system.

`sensors -j` is only a formatter over /sys/class/hwmon, so read_hwmon_sysfs()
builds the identical structure straight from sysfs. The rest of the app can then
treat lm_sensors and raw sysfs as interchangeable sources.
"""

import os
import re
import glob
import subprocess
import threading
from dataclasses import dataclass, field

from . import log

# Commands currently running, so shutdown() can kill one that is wedged. A read
# happens on the worker thread while the GUI thread is the one closing, so this
# is touched from both.
_running = set()
_running_lock = threading.Lock()
_stopped = threading.Event()


def shutdown():
    """Refuse new commands and kill any in flight.

    Closing the window while `sensors` or `nvidia-smi` is mid-call otherwise
    means waiting out its whole timeout before the worker thread can finish,
    and Qt aborts if the thread outlives the window.
    """
    _stopped.set()
    with _running_lock:
        for proc in list(_running):
            try:
                proc.kill()
            except OSError:
                pass


def run_cmd(argv, timeout=5.0):
    """Run a command and return its stdout, or None if it fails.

    Every external command gets a timeout: these run on a 1 second GUI timer,
    and a wedged nvidia-smi or sensors call would otherwise freeze the whole
    interface with no indication of why.
    """
    if _stopped.is_set():
        return None

    try:
        proc = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.debug("Command not found: %s", argv[0])
        return None
    except OSError as exc:
        log.debug("Could not run %s: %s", " ".join(argv), exc)
        return None

    with _running_lock:
        _running.add(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        log.warning("Command timed out after %.1fs: %s", timeout, " ".join(argv))
        return None
    finally:
        with _running_lock:
            _running.discard(proc)

    if proc.returncode != 0:
        # Killed by shutdown(), or the command genuinely failed.
        log.debug("Command exited %s: %s", proc.returncode, " ".join(argv))
        return None
    return out


def nv_value(text):
    """Parse one nvidia-smi CSV field, returning None when it has no reading."""
    text = (text or "").strip()
    if not text or text.startswith("[") or text in ("N/A", "Unknown"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# sysfs reports these in thousandths (or millionths) of the unit lm_sensors
# uses. energy is a cumulative microjoule counter, scaled to joules here so a
# rate over it comes out in watts.
_HWMON_SCALE = {"temp": 1000.0, "in": 1000.0, "curr": 1000.0,
                "power": 1_000_000.0, "energy": 1_000_000.0}

# The attributes that carry a reading, as opposed to limits and alarms.
_VALUE_ATTRS = ("input", "average", "cap")

_ATTR_RE = re.compile(r"^([a-z]+)([0-9]+)_([a-z_]+)$")


@dataclass
class HwmonSensor:
    """One sensor of one hwmon chip: temp2, energy1, fan3 and so on.

    The kind comes from the attribute name, which the hwmon ABI fixes, rather
    than from the label, which the driver makes up. xe labels a temperature, a
    voltage and an energy counter all "pkg", so a label says what a sensor
    measures only by coincidence.
    """

    kind: str                   # "temp", "in", "curr", "power", "energy", ...
    index: int                  # the N in tempN
    label: str                  # the driver's label, or the raw name
    labelled: bool              # whether the driver published a label at all
    values: dict = field(default_factory=dict)   # "input" -> scaled value

    @property
    def raw(self):
        return "%s%d" % (self.kind, self.index)

    def value(self, *attrs):
        """The first of these attributes the driver published, else None."""
        for attr in attrs:
            if attr in self.values:
                return self.values[attr]
        return None


def hwmon_sensors(hwmon):
    """Every sensor in one hwmon directory, in a stable order."""
    found = {}
    for attr in _VALUE_ATTRS:
        for path in glob.glob(os.path.join(hwmon, "*_%s" % attr)):
            match = _ATTR_RE.match(os.path.basename(path))
            if not match or match.group(3) != attr:
                continue
            kind, index = match.group(1), int(match.group(2))
            try:
                with open(path) as f:
                    value = float(f.read().strip())
            except (OSError, ValueError):
                continue                        # unreadable or "N/A"

            sensor = found.get((kind, index))
            if sensor is None:
                label, labelled = "%s%d" % (kind, index), False
                try:
                    with open(os.path.join(hwmon, "%s%d_label"
                                           % (kind, index))) as f:
                        text = f.read().strip()
                    if text:
                        label, labelled = text, True
                except OSError:
                    pass
                sensor = HwmonSensor(kind, index, label, labelled)
                found[(kind, index)] = sensor
            sensor.values[attr] = value / _HWMON_SCALE.get(kind, 1.0)
    return [found[key] for key in sorted(found)]


def read_hwmon_dir(hwmon):
    """Read one hwmon directory into {label: {attribute: scaled value}}.

    Split out so a provider can read the hwmon node that belongs to a specific
    device - a GPU card or a disk - instead of trying to work out which entry
    of the global snapshot belongs to it.

    The shape is what `sensors -j` produces, keyed by label. Where a driver
    gives two sensors the same label - xe calls a temperature, a voltage and an
    energy counter all "pkg" - each is keyed by label and raw name instead,
    "pkg (temp2)", because merging them would hand whichever came first to a
    caller asking for the label's input.
    """
    sensors = hwmon_sensors(hwmon)
    counts = {}
    for sensor in sensors:
        counts[sensor.label] = counts.get(sensor.label, 0) + 1

    readings = {}
    for sensor in sensors:
        label = sensor.label
        if counts[label] > 1:
            label = "%s (%s)" % (label, sensor.raw)
        readings[label] = {"%s_%s" % (sensor.raw, attr): value
                           for attr, value in sensor.values.items()}
    return readings


def read_hwmon_sysfs():
    """Build the same structure `sensors -j` returns, directly from sysfs.

    `sensors` is only a formatter over /sys/class/hwmon, so reading those files
    lets the app keep showing temperatures, fans and power when lm_sensors is
    not installed, instead of presenting an empty window.
    """
    data = {}
    for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(hwmon, "name")) as f:
                chip = f.read().strip()
        except OSError:
            continue
        if not chip:
            continue

        readings = read_hwmon_dir(hwmon)
        if readings:
            readings["Adapter"] = "sysfs"
            data["%s-hwmon%s" % (chip, hwmon.rsplit("hwmon", 1)[-1])] = readings
    return data
