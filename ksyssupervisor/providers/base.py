"""Provider framework.

A provider owns one hardware subsystem: it reports the devices it found and,
on each tick, a flat list of Readings. Providers never touch Qt, so they can run
on a worker thread and be tested against recorded sensor fixtures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# Display order of the groups within a device's category.
GROUP_ORDER = ["Temperatures", "Utilization", "Clocks", "Powers", "Voltages",
               "Fans", "VRAM", "Charge"]


@dataclass(frozen=True)
class Device:
    """A hardware item that gets its own top-level category."""

    key: str            # stable identity, e.g. "cpu", "gpu:0000:03:00.0"
    name: str           # display name, e.g. "AMD Ryzen 7 9700X"
    order: int = 50     # sort hint for the default category order


@dataclass(frozen=True)
class Reading:
    """One sensor value for one tick."""

    device: str                     # Device.key this belongs to
    group: str                      # one of GROUP_ORDER
    key: str                        # unique within the device
    label: str                      # row label, e.g. "Hot Spot"
    value: float
    kind: str                       # a KSysSupervisor.SENSOR_TYPES key
    total: Optional[float] = None   # renders as "used / total" when set

    @property
    def uid(self):
        return "%s/%s" % (self.device, self.key)


class ReadContext:
    """Per-tick input handed to a provider.

    `chips` holds only the hwmon chips this provider claimed, in the structure
    `sensors -j` produces: {chip_name: {label: {attribute: value}}}.
    """

    def __init__(self, chips=None, slots=None, backend=""):
        self.chips = chips or {}
        self.slots = slots or {}      # chip name -> ordinal among same-type chips
        self.backend = backend
        self.issues = {}

    def issue(self, key, message):
        """Record a non-fatal problem; the UI reports each key only once."""
        self.issues.setdefault(key, message)

    @staticmethod
    def sensors(chip_data):
        """Yield (label, attributes), skipping the Adapter entry and non-dicts."""
        for label, attrs in chip_data.items():
            if label == "Adapter" or not isinstance(attrs, dict):
                continue
            yield label, attrs

    @staticmethod
    def first(attrs, *needles):
        """First attribute whose name contains any needle, else None.

        Replaces the repeated `for k, v in attrs.items(): if 'input' in k`
        pattern; each label carries at most one reading of a given kind.
        """
        for name, value in attrs.items():
            if any(n in name for n in needles):
                return value
        return None


class EnergyMeter:
    """Watts from cumulative energy counters, one reading per tick.

    RAPL and the i915/xe hwmon nodes publish energy, not power: a counter that
    only ever grows. Power is its delta over the time between two reads, so
    the first read of each counter only establishes a baseline and reports
    nothing. Shown directly, the counter is a number that climbs forever.
    """

    def __init__(self):
        self._last = {}          # key -> (joules, monotonic timestamp)

    def rate(self, key, joules, now=None, wrap=None):
        """Watts since the last call for this key, or None on the first.

        `wrap` is the counter's range in joules, when it has a known one. A
        counter that goes backwards without one was reset - the driver was
        reloaded, or the machine resumed - and that tick is skipped rather
        than reported as a huge negative power.
        """
        if now is None:
            now = time.monotonic()
        previous = self._last.get(key)
        self._last[key] = (joules, now)
        if previous is None:
            return None

        last_joules, last_time = previous
        elapsed = now - last_time
        if elapsed <= 0:
            return None
        delta = joules - last_joules
        if delta < 0:
            if wrap is None:
                return None
            delta += wrap
        return delta / elapsed


@dataclass(frozen=True)
class Step:
    """One shell command, with a note on what that particular line does."""

    command: str
    note: str = ""


@dataclass(frozen=True)
class Advice:
    """A piece of hardware the machine has but cannot currently read.

    Structured rather than pre-formatted text so the UI can lay the commands
    out properly and show what each one achieves, instead of squeezing a shell
    snippet into a message box.
    """

    key: str                 # stable id, also used to de-duplicate
    title: str               # short imperative summary
    problem: str             # what is missing right now
    effect: str              # what the user gains by fixing it
    steps: tuple = ()        # Step, run in order
    steps_title: str = "Run these commands"   # false when they are alternatives
    result: str = ""         # how to confirm it worked
    persist: tuple = ()      # optional Step to survive a reboot
    persist_note: str = ""   # what happens on reboot; required when no persist

    def as_text(self):
        """Plain-text rendering, for the log and the diagnostics report."""
        lines = [self.title, "  Problem: %s" % self.problem,
                 "  Gains:   %s" % self.effect]
        if self.steps:
            lines.append("  %s:" % self.steps_title)
        for step in self.steps:
            lines.append("    $ %s" % step.command)
            if step.note:
                lines.append("      %s" % step.note)
        if self.persist:
            lines.append("  To survive a reboot:")
            for step in self.persist:
                lines.append("    $ %s" % step.command)
                if step.note:
                    lines.append("      %s" % step.note)
        if self.persist_note:
            lines.append("  After a reboot: %s" % self.persist_note)
        if self.result:
            lines.append("  Then: %s" % self.result)
        return "\n".join(lines)


class SensorProvider:
    """Base class. Subclasses override what they actually support."""

    name = "provider"
    chips = frozenset()      # hwmon chip base names claimed from the snapshot
    takes_leftovers = False  # receives every chip no other provider claimed

    def claims(self, chip):
        """True if this provider wants the hwmon chip with this base name."""
        return chip in self.chips

    def devices(self):
        """Devices this provider discovered. Called once at startup."""
        return []

    def read(self, ctx):
        """Yield Readings for this tick."""
        return []

    def advice(self):
        """Setup guidance when hardware is present but unreadable."""
        return []
