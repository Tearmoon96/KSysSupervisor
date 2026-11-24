"""Drive health: SMART data turned into a verdict and the reasons for it.

The data comes from UDisks2, which reads every drive's SMART log as root and
republishes it, so nothing here needs a password. An NVMe drive reports its
wear in a field the standard defines ("percentage used"). A SATA drive does
not: wear is a vendor attribute whose number differs per maker, so it is taken
from the attribute that maker is known to use and, where the maker is not
known, reported as unknown rather than read off a number that might mean
something else.

Qt-free. `read_all()` is what the window calls, on a thread.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import APP_NAME, log, udisks

GOOD = "good"
CAUTION = "caution"
BAD = "bad"
UNKNOWN = "unknown"

STATUS_TITLES = {GOOD: "Good", CAUTION: "Caution", BAD: "Bad",
                 UNKNOWN: "Unknown"}

#: Life left, in percent, at or below which a healthy drive earns a caution.
LOW_LIFE = 10

# UDisks' "pretty" units for an ATA attribute's raw value.
UNIT_UNKNOWN, UNIT_NONE, UNIT_MSECONDS, UNIT_SECTORS, UNIT_MKELVIN = range(5)

# The ATA attributes the verdict is built on. These IDs mean the same thing on
# every drive that has them.
ATTR_REALLOCATED = 5
ATTR_POWER_ON = 9
ATTR_POWER_CYCLES = 12
ATTR_REPORTED_UNCORRECTABLE = 187
ATTR_PENDING = 197
ATTR_UNCORRECTABLE = 198
ATTR_CRC = 199
ATTR_LBAS_WRITTEN = 241
ATTR_LBAS_READ = 242

#: Which attribute's *normalised value* is "percent of rated life left", by the
#: drive models that use it that way. Matched against the start of the model
#: string, upper-cased. Only makers whose usage is documented are listed: the
#: same number is a different counter elsewhere (202 is a data address mark
#: error count on old hard disks), so there is no generic fallback for these.
WEAR_ATTRIBUTES = (
    (("CT", "CRUCIAL", "MICRON", "MTFD"), 202),
    (("SAMSUNG",), 177),
    (("INTEL", "SSDSC", "SOLIDIGM"), 233),
    (("KINGSTON",), 231),
    (("WDC", "WD ", "SANDISK"), 230),
    (("ADATA", "PATRIOT", "TEAM", "SILICON POWER", "SPCC"), 231),
)

#: Attributes that mean "life left" on enough unrelated drives to be worth
#: showing for an unlisted maker - marked as an estimate when used.
GENERIC_WEAR_ATTRIBUTES = (231, 169)

#: Names for attributes UDisks leaves numbered or names after what the number
#: meant on hard disks, per model prefix. Display only.
VENDOR_NAMES = (
    (("CT", "CRUCIAL", "MICRON", "MTFD"), {
        173: "Average block erase count",
        174: "Unexpected power loss count",
        180: "Unused reserve blocks",
        202: "Percent lifetime remaining",
        206: "Write error rate",
        210: "RAIN successful recoveries",
        246: "Total LBAs written",
        247: "Host program page count",
        248: "FTL program page count",
    }),
    (("SAMSUNG",), {
        177: "Wear levelling count",
        179: "Used reserved blocks",
        181: "Program fail count",
        182: "Erase fail count",
        235: "Power loss recovery count",
    }),
)

#: What an NVMe controller's critical-warning flags mean, as UDisks names them.
NVME_WARNINGS = {
    "spare": "The spare area has run below its threshold.",
    "temperature": "The drive is outside its safe temperature range.",
    "degraded": "The drive reports that its reliability is degraded.",
    "readonly": "The drive has switched itself to read-only.",
    "volatile_mem": "The drive's volatile memory backup has failed.",
    "pmr_readonly": "The persistent memory region has become read-only.",
}


@dataclass(frozen=True)
class Attribute:
    """One row of a SATA drive's SMART table."""
    id: int
    name: str
    value: int              # normalised, higher is healthier
    worst: int
    threshold: int
    raw: Optional[int]      # in `unit`, None when UDisks cannot interpret it
    unit: int

    @property
    def failing(self):
        """Below the maker's own failure line right now."""
        return self.threshold > 0 and 0 <= self.value <= self.threshold

    @property
    def raw_text(self):
        if self.raw is None:
            return "-"
        if self.unit == UNIT_MSECONDS:
            return "%d h" % (self.raw // 3600000)
        if self.unit == UNIT_MKELVIN:
            return "%d °C" % round(self.raw / 1000.0 - 273.15)
        if self.unit == UNIT_SECTORS:
            return "%d sectors" % self.raw
        return "%d" % self.raw


@dataclass
class Health:
    status: str = UNKNOWN
    reasons: list = field(default_factory=list)   # why not Good
    notes: list = field(default_factory=list)     # worth knowing, not a fault
    kind: str = ""                                # "ata", "nvme" or ""
    temperature: Optional[int] = None             # °C
    power_on_hours: Optional[int] = None
    power_cycles: Optional[int] = None
    life_left: Optional[int] = None               # percent
    life_note: str = ""                           # where it came from
    written: Optional[int] = None                 # bytes
    read: Optional[int] = None
    reallocated: Optional[int] = None
    pending: Optional[int] = None
    uncorrectable: Optional[int] = None
    crc_errors: Optional[int] = None
    spare: Optional[int] = None                   # NVMe, percent
    spare_threshold: Optional[int] = None
    media_errors: Optional[int] = None
    unsafe_shutdowns: Optional[int] = None
    rotation_rate: Optional[int] = None           # rpm, 0 for solid state
    selftest: str = ""
    selftest_remaining: Optional[int] = None      # percent, while one runs
    drive_path: str = ""                          # the disk service's object
    interface: str = ""                           # and its SMART interface
    updated: Optional[int] = None                 # epoch seconds
    attributes: list = field(default_factory=list)

    @property
    def title(self):
        return STATUS_TITLES[self.status]


def unknown(reason):
    return Health(status=UNKNOWN, reasons=[reason])


def _vendor_table(model, table):
    model = " ".join(model.upper().split())
    for prefixes, value in table:
        if model.startswith(prefixes):
            return value
    return None


def _pretty_name(attribute_id, name, model):
    names = _vendor_table(model, VENDOR_NAMES) or {}
    if attribute_id in names:
        return names[attribute_id]
    if name.startswith("attribute-"):
        return "Attribute %d" % attribute_id
    # UDisks numbers a second meaning of the same name: "temperature-celsius-2".
    words = [w for w in name.split("-") if not w.isdigit()]
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else "Attribute %d" % attribute_id


def parse_ata_attributes(rows, model=""):
    """[Attribute] from UDisks' SmartGetAttributes reply for a SATA drive."""
    found = []
    for row in rows or ():
        try:
            attribute_id, name, _flags, value, worst, threshold, pretty, unit = \
                row[:8]
            unit = int(unit)
            found.append(Attribute(
                id=int(attribute_id),
                name=_pretty_name(int(attribute_id), str(name), model),
                value=int(value), worst=int(worst), threshold=int(threshold),
                # With no known unit UDisks reports 0, which is "cannot
                # interpret", not a count of zero.
                raw=None if unit == UNIT_UNKNOWN else int(pretty),
                unit=unit))
        except (TypeError, ValueError, IndexError):
            continue
    return found


def _raw(attributes, attribute_id):
    for attribute in attributes:
        if attribute.id == attribute_id:
            return attribute.raw
    return None


def _celsius(kelvin):
    try:
        kelvin = float(kelvin)
    except (TypeError, ValueError):
        return None
    if kelvin <= 0:
        return None
    return int(round(kelvin - 273.15))


def ata_wear(attributes, model):
    """(percent of life left, note) for a SATA SSD, or (None, why not)."""
    by_id = {a.id: a for a in attributes}
    known = _vendor_table(model, WEAR_ATTRIBUTES)
    if known is not None and known in by_id:
        value = by_id[known].value
        if 0 <= value <= 100:
            return value, "SMART attribute %d" % known
    for attribute_id in GENERIC_WEAR_ATTRIBUTES:
        attribute = by_id.get(attribute_id)
        if attribute is not None and 0 <= attribute.value <= 100:
            return attribute.value, (
                "estimated from SMART attribute %d, whose meaning is not "
                "confirmed for this model" % attribute_id)
    return None, ("This drive does not publish its wear in a SMART attribute "
                  "known for this model.")


def _judge(health, failing_now, failed_before, overall_failing):
    """Fill in status and reasons from the numbers already in `health`."""
    bad, caution = [], []

    if overall_failing:
        bad.append("The drive itself reports that it is failing (SMART "
                   "overall status). Back up what is on it now.")
    for attribute in failing_now:
        bad.append("%s is below the failure threshold its maker set "
                   "(%d, threshold %d)."
                   % (attribute.name, attribute.value, attribute.threshold))
    if (health.spare is not None and health.spare_threshold is not None
            and health.spare < health.spare_threshold):
        bad.append("Spare area is down to %d%%, below the drive's own "
                   "threshold of %d%%."
                   % (health.spare, health.spare_threshold))
    if health.life_left is not None and health.life_left <= 0:
        bad.append("The drive has used up its rated life.")

    def counted(value, one, many):
        if value:
            caution.append((one if value == 1 else many) % value)

    counted(health.reallocated,
            "%d sector went bad and was replaced by a spare.",
            "%d sectors went bad and were replaced by spares.")
    counted(health.pending,
            "%d sector could not be read and is waiting to be replaced.",
            "%d sectors could not be read and are waiting to be replaced.")
    counted(health.uncorrectable,
            "%d sector is unreadable and could not be corrected.",
            "%d sectors are unreadable and could not be corrected.")
    counted(health.media_errors,
            "%d data error the drive could not correct.",
            "%d data errors the drive could not correct.")
    if health.life_left is not None and 0 < health.life_left <= LOW_LIFE:
        caution.append("Only %d%% of the drive's rated life is left."
                       % health.life_left)
    if failed_before and not failing_now:
        caution.append("An attribute has been below its failure threshold "
                       "in the past.")
    # The disk service's names for a failed test: "fatal" and "error_..."
    # ("aborted" and "interrupted" are a test that was stopped, not a fault).
    outcome = (health.selftest or "").lower()
    if outcome == "fatal" or outcome.startswith("error") or "fail" in outcome:
        caution.append("The drive's last self-test failed (%s)."
                       % SELFTEST_TEXTS.get(outcome, health.selftest))

    if health.crc_errors:
        health.notes.append(
            "%d interface CRC errors on record. These come from the cable or "
            "connector, not from the drive's own storage." % health.crc_errors)

    if bad:
        health.status, health.reasons = BAD, bad + caution
    elif caution:
        health.status, health.reasons = CAUTION, caution
    else:
        health.status, health.reasons = GOOD, []
    return health


def from_ata(ata, rows, model="", rotation_rate=None, logical_sector=512):
    """Health of a SATA drive from UDisks' Drive.Ata properties and table."""
    if not ata.get("SmartSupported", False):
        return unknown("This drive does not support SMART.")
    if not ata.get("SmartEnabled", False):
        return unknown("SMART is switched off on this drive.")
    if not ata.get("SmartUpdated"):
        return unknown("The drive's SMART data has not been read yet.")

    attributes = parse_ata_attributes(rows, model)
    health = Health(kind="ata", attributes=attributes,
                    rotation_rate=rotation_rate,
                    updated=int(ata.get("SmartUpdated") or 0) or None,
                    selftest=str(ata.get("SmartSelftestStatus") or ""))
    health.temperature = _celsius(ata.get("SmartTemperature"))
    seconds = ata.get("SmartPowerOnSeconds")
    if seconds:
        health.power_on_hours = int(seconds) // 3600
    health.power_cycles = _raw(attributes, ATTR_POWER_CYCLES)
    health.reallocated = _raw(attributes, ATTR_REALLOCATED)
    health.pending = _raw(attributes, ATTR_PENDING)
    health.uncorrectable = _raw(attributes, ATTR_UNCORRECTABLE)
    health.crc_errors = _raw(attributes, ATTR_CRC)
    for attribute_id, target in ((ATTR_LBAS_WRITTEN, "written"),
                                 (ATTR_LBAS_READ, "read")):
        lbas = _raw(attributes, attribute_id)
        if lbas:
            setattr(health, target, lbas * logical_sector)

    if rotation_rate == 0:
        health.life_left, health.life_note = ata_wear(attributes, model)

    return _judge(
        health,
        failing_now=[a for a in attributes if a.failing],
        failed_before=int(ata.get("SmartNumAttributesFailedInThePast") or 0) > 0,
        overall_failing=bool(ata.get("SmartFailing", False)))


def from_nvme(controller, attributes):
    """Health of an NVMe drive from UDisks' controller properties and log."""
    if not controller.get("SmartUpdated"):
        return unknown("The drive's SMART data has not been read yet.")
    attributes = attributes if isinstance(attributes, dict) else {}

    health = Health(kind="nvme", rotation_rate=0,
                    updated=int(controller.get("SmartUpdated") or 0) or None,
                    selftest=str(controller.get("SmartSelftestStatus") or ""))
    health.temperature = _celsius(controller.get("SmartTemperature"))
    hours = controller.get("SmartPowerOnHours")
    health.power_on_hours = int(hours) if hours is not None else None

    def number(key):
        value = attributes.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    health.power_cycles = number("power_cycles")
    health.spare = number("avail_spare")
    health.spare_threshold = number("spare_thresh")
    health.media_errors = number("media_errors")
    health.unsafe_shutdowns = number("unsafe_shutdowns")
    health.written = number("total_data_written")
    health.read = number("total_data_read")
    used = number("percent_used")
    if used is not None:
        # The standard lets this run past 100 on a drive still working beyond
        # its rating; life left stops at zero.
        health.life_left = max(0, 100 - used)
        health.life_note = "NVMe percentage used: %d%%" % used

    health = _judge(health, failing_now=[], failed_before=False,
                    overall_failing=False)
    warnings = [w for w in controller.get("SmartCriticalWarning") or ()]
    if warnings:
        health.status = BAD
        health.reasons = [NVME_WARNINGS.get(
            w, "The drive raises a critical warning (%s)." % w)
            for w in warnings] + health.reasons
    return health


NO_SERVICE = ("Health data comes from the UDisks2 disk service, which did not "
              "answer. It is installed by every desktop distribution "
              "(package 'udisks2').")
ON_STICK = ("Drive health is not read on the recovery stick: it comes from "
            "the UDisks2 disk service of an installed system. Start the "
            "computer normally to see it.")
NO_SMART = ("This device does not expose SMART data. That is normal for "
            "memory sticks, card readers and many USB enclosures.")


def read_all(disk_list, refresh=False, run=None):
    """{disk name: Health} for the given drives. Never raises.

    `refresh` asks UDisks to read each drive again first; without it the data
    is whatever UDisks last collected, which it does every ten minutes.
    """
    if udisks.on_stick():
        return {disk.name: unknown(ON_STICK) for disk in disk_list}
    try:
        managed = udisks.objects(run=run)
    except udisks.UDisksError as exc:
        log.info("Disk health unavailable: %s", exc)
        return {disk.name: unknown(NO_SERVICE) for disk in disk_list}

    paths = udisks.drive_paths(managed)
    if refresh:
        updated = False
        for disk in disk_list:
            path = paths.get(disk.name)
            if path and udisks.smart_update(path, managed.get(path, {}), run):
                updated = True
        if updated:
            try:
                managed = udisks.objects(run=run)
            except udisks.UDisksError:
                pass

    found = {}
    for disk in disk_list:
        try:
            found[disk.name] = _read_one(disk, paths.get(disk.name), managed,
                                         run)
        except Exception:
            log.exception("Could not read the health of %s", disk.name)
            found[disk.name] = unknown("The drive's health data could not be "
                                       "read.")
    return found


def _read_one(disk, path, managed, run):
    interfaces = managed.get(path) if path else None
    if not interfaces:
        return unknown(NO_SMART)
    drive = interfaces.get(udisks.DRIVE, {})
    model = str(drive.get("Model") or disk.model)

    if udisks.NVME in interfaces:
        health = from_nvme(interfaces[udisks.NVME],
                           udisks.smart_attributes(path, interfaces, run))
        return _with_selftest(health, path, udisks.NVME,
                              interfaces[udisks.NVME])
    if udisks.ATA in interfaces:
        rate = drive.get("RotationRate")
        if rate is None or int(rate) < 0:
            # UDisks could not tell; the kernel's own flag is the next best.
            rate = None if disk.rotational else 0
        health = from_ata(interfaces[udisks.ATA],
                          udisks.smart_attributes(path, interfaces, run),
                          model=model, rotation_rate=rate,
                          logical_sector=disk.logical_sector)
        return _with_selftest(health, path, udisks.ATA,
                              interfaces[udisks.ATA])
    return unknown(NO_SMART)


def _with_selftest(health, path, interface, properties):
    """Where to ask for a self-test, and how far a running one has got."""
    health.drive_path, health.interface = path, interface
    try:
        remaining = int(properties.get("SmartSelftestPercentRemaining"))
    except (TypeError, ValueError):
        remaining = None
    if remaining is not None and remaining >= 0 \
            and "inprogress" in health.selftest.lower().replace(" ", ""):
        health.selftest_remaining = remaining
    return health


SELFTEST_TEXTS = {
    "success": "Passed", "aborted": "Stopped before it finished",
    "interrupted": "Interrupted (the drive was reset or powered off)",
    "fatal": "Could not complete", "error_unknown": "Failed",
    "error_electrical": "Failed: electrical fault",
    "error_servo": "Failed: servo or seek fault",
    "error_read": "Failed: read error",
    "error_handling": "Failed: possible handling damage",
    "inprogress": "Running",
}


def selftest_text(health):
    """The last self-test's outcome in words, or "" when there is none."""
    key = (health.selftest or "").lower()
    if not key:
        return ""
    text = SELFTEST_TEXTS.get(key, health.selftest)
    if key == "inprogress" and health.selftest_remaining is not None:
        text = "Running, %d%% to go" % health.selftest_remaining
    return text


def age_text(updated, now=None):
    """"3 minutes ago" for a SMART reading's timestamp."""
    if not updated:
        return ""
    seconds = max(0, int((now or time.time()) - updated))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return "%d minutes ago" % round(seconds / 60.0)
    if seconds < 129600:
        return "%d hours ago" % round(seconds / 3600.0)
    return "%d days ago" % round(seconds / 86400.0)


# ---- keeping watch ---------------------------------------------------------------

RANK = {UNKNOWN: 0, GOOD: 0, CAUTION: 1, BAD: 2}


def state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state")
    return os.path.join(base, APP_NAME, "disk-health.json")


def desktop_notify(title, text, critical=False):
    """A desktop notification, where the desktop has a way to show one."""
    if shutil.which("notify-send") is None:
        log.warning("%s: %s", title, text)
        return
    try:
        subprocess.run(
            ["notify-send", "--app-name", APP_NAME, "--icon",
             "drive-harddisk", "--urgency",
             "critical" if critical else "normal", title, text],
            timeout=30, check=False, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Could not show a notification: %s", exc)


class HealthWatch:
    """Every drive's health, kept at hand and watched for getting worse.

    Reading it takes the disk service a moment, so it is done on a thread of
    its own every ten minutes - as often as the service reads the drives -
    and whoever wants the numbers takes the last ones with snapshot().

    A drive whose verdict is worse than the last one it was reported at
    raises one desktop notification: drives tend to give some warning, and
    the warning is only worth something while the data can still be copied.
    What was last reported is remembered across runs, so a drive that has
    had one bad sector for years does not announce it at every start.
    """

    INTERVAL = 600

    def __init__(self, inventory=None, read=None, notify=None, path=None,
                 interval=INTERVAL):
        from . import disks
        self._inventory = inventory or disks.inventory
        self._read = read or read_all
        self._notify = notify or desktop_notify
        self._path = path or state_path()
        self._interval = interval
        self._health = {}
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop,
                                            name="disk-health", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                log.exception("Could not read drive health")
            self._stop.wait(self._interval)

    def snapshot(self):
        """{block device name: Health}, as last read."""
        return self._health

    @staticmethod
    def _identity(disk):
        return disk.serial or disk.wwn or "%s/%s" % (disk.model, disk.name)

    def _remembered(self):
        try:
            with open(self._path, encoding="utf-8") as handle:
                found = json.load(handle)
            return found if isinstance(found, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, state):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path + ".new", "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            os.replace(self._path + ".new", self._path)
        except OSError as exc:
            log.warning("Could not save %s: %s", self._path, exc)

    def poll(self):
        """Read every drive once; notify about any that got worse."""
        drives = [d for d in self._inventory() if d.has_media]
        found = self._read(drives)
        self._health = found
        before = self._remembered()
        after = dict(before)
        for disk in drives:
            health = found.get(disk.name)
            if health is None:
                continue
            key = self._identity(disk)
            rank = RANK.get(health.status, 0)
            if health.status == UNKNOWN and key in before:
                continue                 # no data this time: keep what was
            if rank > int(before.get(key, 0)):
                reason = health.reasons[0] if health.reasons else ""
                self._notify(
                    "Drive health: %s" % health.title,
                    "%s (%s). %s Open the Disk Utility for details%s."
                    % (disk.title, disk.name, reason,
                       ", and copy what matters off this drive now"
                       if health.status == BAD else ""),
                    health.status == BAD)
            after[key] = rank
        if after != before:
            self._remember(after)
        return found
