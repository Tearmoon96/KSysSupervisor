"""Recent history of every reading, for the live graphs.

Qt-free, like the providers: what is kept and how an axis is scaled are
decisions that can be tested without a display.

Every reading is recorded from the moment the app starts, not only the ones
being graphed. A graph added later then opens onto the last few minutes rather
than onto an empty strip - which is when a graph is usually wanted: after
noticing that something just happened.
"""

import bisect
import math
import time
from collections import deque

#: How far back the graphs can look. The longest span the panel offers.
MAX_AGE = 10 * 60

#: A ceiling on ticks kept whatever the interval, so --interval 0.2 cannot
#: grow the history five-fold.
MAX_TICKS = 3600

#: What the panel offers, in seconds.
SPANS = (60, 300, 600)
DEFAULT_SPAN = 300

GAP = float("nan")

#: Kinds read on a fixed 0..100 scale: a percentage auto-scaled to 3..5 turns
#: noise into mountains.
PERCENT_KINDS = frozenset(("utilization",))

#: The smallest range an auto-scaled axis may show, per kind. A temperature
#: holding at 45.0 would otherwise fill the strip with its own rounding.
MIN_SPAN = {"temp": 10.0, "fan": 500.0, "clock": 200.0, "power": 10.0,
            "voltage": 0.2, "memory_gb": 1.0, "memory": 256.0}

#: Kinds that cannot go below zero, so an axis padded under them stops at 0.
NON_NEGATIVE = frozenset(("fan", "clock", "power", "memory_gb", "memory",
                          "utilization"))


class SeriesStore:
    """A rolling window of every reading, aligned on shared tick times.

    One list of timestamps for all readings, and one of values per reading,
    aligned from the end: values[-k] belongs to times[-k]. A reading that
    appears late simply has a shorter list; one that misses a tick records a
    gap, so its line breaks rather than bridging a stretch nobody measured.
    """

    def __init__(self, max_age=MAX_AGE, max_ticks=MAX_TICKS):
        self.max_age = max_age
        self.max_ticks = max_ticks
        self._times = deque()
        self._values = {}           # uid -> deque of floats (GAP = no reading)
        self._kinds = {}            # uid -> kind
        self._totals = {}           # uid -> total, for used/total readings

    def record(self, readings, now=None):
        """Add one tick. `readings` are providers.base.Reading-like."""
        now = time.time() if now is None else now
        self._times.append(now)
        seen = set()
        for reading in readings:
            values = self._values.get(reading.uid)
            if values is None:
                values = self._values[reading.uid] = deque()
            if reading.uid in seen:
                continue
            seen.add(reading.uid)
            values.append(_number(reading.value))
            self._kinds[reading.uid] = reading.kind
            if reading.total:
                self._totals[reading.uid] = reading.total
        for uid, values in self._values.items():
            if uid not in seen:
                values.append(GAP)
        self._trim(now)

    def _trim(self, now):
        cutoff = now - self.max_age
        while self._times and (len(self._times) > self.max_ticks
                               or self._times[0] < cutoff):
            self._times.popleft()
        keep = len(self._times)
        for uid in list(self._values):
            values = self._values[uid]
            while len(values) > keep:
                values.popleft()
            if not values or all(math.isnan(v) for v in values):
                # Gone for the whole window: nothing left worth holding.
                del self._values[uid]
                self._kinds.pop(uid, None)
                self._totals.pop(uid, None)

    def points(self, uid, span, now=None):
        """[(time, value)] within the last `span` seconds, gaps as NaN."""
        values = self._values.get(uid)
        if not values:
            return []
        now = time.time() if now is None else now
        times = list(self._times)[-len(values):]
        start = bisect.bisect_left(times, now - span)
        return list(zip(times[start:], list(values)[start:]))

    def kind(self, uid):
        return self._kinds.get(uid)

    def total(self, uid):
        return self._totals.get(uid)

    def clear(self):
        self._times.clear()
        self._values.clear()
        self._kinds.clear()
        self._totals.clear()


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return GAP
    return number if math.isfinite(number) else GAP


def finite(points):
    """The values of `points` that are actual readings."""
    return [v for _t, v in points if not math.isnan(v)]


def nice_step(raw):
    """A round grid step near `raw`: 1, 2, 2.5 or 5 times a power of ten."""
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def axis_range(values, kind, total=None, lines=4):
    """(low, high, step) for a strip's vertical axis.

    Percentages are always 0..100 and an amount with a known total is 0..total:
    both have a natural scale, and a graph that re-scales under them makes a
    steady load look like it is moving. Everything else fits the data, padded
    and widened to at least MIN_SPAN, on round grid lines.
    """
    if kind in PERCENT_KINDS:
        return 0.0, 100.0, 25.0
    if total:
        return 0.0, float(total), nice_step(total / lines)

    values = [v for v in values if math.isfinite(v)]
    if not values:
        return 0.0, 1.0, 0.25
    low, high = min(values), max(values)
    wanted = MIN_SPAN.get(kind, 1.0)
    if high - low < wanted:
        middle = (high + low) / 2.0
        low, high = middle - wanted / 2.0, middle + wanted / 2.0
    pad = (high - low) * 0.1
    low, high = low - pad, high + pad
    if kind in NON_NEGATIVE and min(values) >= 0:
        low = max(0.0, low)

    step = nice_step((high - low) / lines)
    low = math.floor(low / step) * step
    high = math.ceil(high / step) * step
    return low, high, step


def nearest(points, when):
    """Index of the point closest in time to `when`, or None."""
    if not points:
        return None
    times = [t for t, _v in points]
    i = bisect.bisect_left(times, when)
    if i == 0:
        return 0
    if i >= len(times):
        return len(times) - 1
    return i if times[i] - when < when - times[i - 1] else i - 1


#: What a stress test graphs for each kind of hardware it loads: the groups
#: worth watching, and at most how many readings of each. The CPU's clocks are
#: left out - one row per core would bury the few strips that matter - and a
#: card's first utilisation is its load; the next ones (VRAM share, power
#: against its limit) repeat what the memory and power strips already show.
STRESS_PLAN = {
    "cpu": (("Temperatures", 3), ("Utilization", 1), ("Powers", 1)),
    "ram": (("Utilization", 1), ("Temperatures", 2)),
    "gpu": (("Temperatures", 3), ("Utilization", 1), ("Powers", 1),
            ("Clocks", 1), ("Fans", 1)),
}


def stress_graphs(models, device_keys):
    """The reading uids to graph while these devices are under load.

    `models` are the tree's TreeModels, `device_keys` the devices the test
    loads ("cpu", "ram", "gpu:0000:03:00.0"), in the order to show them. Each
    group is taken in the order the tree lists it, and readings that have no
    value right now are skipped rather than given an empty strip.
    """
    by_key = {model.key: model for model in models}
    uids = []
    for key in device_keys:
        model = by_key.get(key)
        plan = STRESS_PLAN.get(key.split(":", 1)[0])
        if model is None or plan is None:
            continue
        groups = dict(model.groups)
        for group, most in plan:
            metrics = [m for m in groups.get(group, ())
                       if m.value is not None]
            uids.extend(m.uid for m in metrics[:most])
    return uids
