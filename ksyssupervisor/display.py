"""What a reading looks like on screen: units, rounding, ranking, staleness.

Qt-free for the same reason the providers are: whether a value leads a tile,
how many decimals it deserves and when a sensor that stopped reporting should
be blanked are all decisions that can be checked without a display. The widgets
render what this module decides and hold no such knowledge themselves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Optional

from .providers import memory
from .providers.base import GROUP_ORDER

#: Shown in place of a value that is absent or has gone stale.
MISSING = "-"

UNITS = {
    "temp": "°C",
    "fan": "RPM",
    "power": "W",
    "voltage": "V",
    "utilization": "%",
    "memory": "MB",
    "memory_gb": "GB",
    "clock": "MHz",
}

# Per kind rather than one rule for everything: the tree rounded every reading
# to one decimal, which spent width on "1240.0 RPM" and "4200.0 MHz" while
# rounding away the millivolts that are the only thing a voltage rail does.
DECIMALS = {
    "temp": 1,
    "fan": 0,
    "power": 1,
    "voltage": 3,
    "utilization": 0,
    "memory": 0,
    "memory_gb": 1,
    "clock": 0,
}

# Labels that read as prose next to a number. Anything not listed is passed
# through: driver-raw names like in0 or AUXTIN1 are identifiers, not prose, and
# renaming them would only hide which register a reading came from.
_SHORT_LABELS = {
    "Processor": "Load",
    "Memory Used": "Used",
    "Power Limit": "Limit",
}

# "GPU" means the load on a utilization reading and the die on a temperature
# one, so it cannot be resolved from the label alone.
_BY_KIND = {
    # "GPU" as a temperature is the edge sensor, and saying so matters now
    # that the hot spot is the one leading the tile: two rows that both read
    # as "the GPU temperature" would be a card contradicting itself.
    "GPU": {"utilization": "Load", "temp": "Edge", "voltage": "Voltage"},
    # SVI2's core rail, as a chip beside the other readings of the CPU.
    "Core": {"voltage": "Voltage"},
    # A card's shader clock; "Graphics" beside a number reads as a name.
    "Graphics": {"clock": "Clock"},
}

# What leads each kind of tile, keyed by Device.order rather than by device
# key: a GPU key carries its PCI address and a drive key its model, so the
# order is the only stable identity a provider already declares.
#
# Two ways to lead. A ring is for a reading with a real top - a share, or an
# amount of a total - where how full it is means something; a chip is for
# everything else. A ring for watts or RPM has nothing honest to fill up to.
# Each ring has two chips beside it, in order, and chips past those fill whole
# lines of two under the rings.
#
# How many readings a slot takes: ONE is the best of that kind, EVERY is all
# of them - a DIMM temperature each, however many sticks - and WAIT is one
# held open with a dash until it first reports. Only the CPU's wattage waits:
# it comes from a delta counter that reports nothing on the first tick, and a
# chip appearing a second later relayouts the tile under the cursor. Anything
# else absent on the first tick will be absent on the tenth, and a permanent
# dash says nothing.
ONE, EVERY, WAIT = "one", "every", "wait"
_TEMPS = ("junction", "hot spot", "Tctl", "Tdie", "edge", "Composite",
          # A board's own sensors, ahead of whatever else hangs off its
          # super-I/O chip: the network card's temperature is not what
          # "mainboard temperature" means to anyone.
          "SYSTIN", "CPUTIN", "temp1", "temp")
_POWERS = ("rapl_Package", "PPT", "power", "power1")


@dataclass(frozen=True)
class Slot:
    """One reading a tile leads with, by kind and preferred keys.

    A locked slot is part of what the tile is - a CPU's load, a card's
    memory - and cannot be moved or hidden in the editor. It is only ever
    filled by its exact key: a card without a load reading should have no
    load ring, not its power-limit percentage standing in for one.
    """

    kind: str
    keys: tuple = ()
    many: str = ONE
    locked: bool = False


@dataclass(frozen=True)
class Layout:
    rings: tuple = ()
    chips: tuple = ()
    #: How an EVERY slot orders what it takes; by label when None.
    rank: object = None


def _dimm_rank(metric):
    """Sticks in the order a board fills its slots: the ones by the ring are
    the ones fitted first."""
    match = re.match(r"^DIMM (\d+)$", metric.label)
    if match is None:
        return (1, _natural(metric.label))
    return (0, memory.fill_rank(int(match.group(1))))


LAYOUTS = {
    # The CPU: its load, and beside it the hottest spot and the clock; under
    # them the core voltage and the package power.
    10: Layout(rings=(Slot("utilization", ("usage_total",), locked=True),),
               chips=(Slot("temp", ("!hotspot",)),
                      Slot("clock", ("!Clocks",)),
                      Slot("voltage", ("SVI2_Core", "!vcore", "vcore",
                                       "core")),
                      Slot("power", _POWERS, WAIT))),
    # RAM: how full it is, and every stick's temperature.
    20: Layout(rings=(Slot("memory_gb", ("usage",), locked=True),),
               chips=(Slot("temp", (), EVERY),), rank=_dimm_rank),
    # A graphics card: load with its hot spot and clock beside it, memory
    # with its voltage and power.
    30: Layout(rings=(Slot("utilization", ("usage_total",), locked=True),
                      Slot("memory_gb", ("vram",), locked=True)),
               chips=(Slot("temp", _TEMPS),
                      Slot("clock", ("sclk", "graphics")),
                      Slot("voltage", ("vddgfx",)),
                      Slot("power", _POWERS))),
    # Drives lead with their volumes and temperatures: see _drive_tile.
    40: Layout(chips=(Slot("temp", _TEMPS),)),
    # A board leads with its hottest sensors: see _board_chips.
    60: Layout(),
    # A battery's charge is the one ring everybody already knows.
    70: Layout(rings=(Slot("utilization", ("capacity",)),),
               chips=(Slot("power", _POWERS),)),
}
DEFAULT_LAYOUT = Layout(chips=(Slot("temp", _TEMPS),))

#: How many chips a board leads with by default, and the range a reading
#: must be in to be one of them. A header with no diode on it reads as
#: nonsense rather than as nothing - -60 °C, 0, 127 - and the hottest of
#: those is not the hottest part of the board.
BOARD_CHIPS = 4
PLAUSIBLE_C = (1.0, 100.0)

#: Most drive volumes shown as rings.
DRIVE_RINGS = 3

#: Volumes that are the boot loader's rather than the user's: shown, but
#: never ahead of a real one.
BOOT_MOUNTS = ("/boot", "/efi")

# What a slot is called while it waits for its reading.
SLOT_CAPTIONS = {"utilization": "Load", "temp": "Temperature",
                 "power": "Power", "memory_gb": "Used", "fan": "Fan"}

#: Where a reading can sit on a tile.
RING = "ring"          # a gauge at the top, for a reading with a maximum
CHIP = "chip"          # a pill at the top
BELOW = "below"        # the rows under them
FOLDED = "folded"      # inside the "More readings" fold
HIDDEN = "hidden"      # nowhere
PLACEMENTS = (RING, CHIP, BELOW, FOLDED, HIDDEN)
PLACEMENT_TITLES = {RING: "Ring", CHIP: "Chip", BELOW: "Below",
                    FOLDED: "More readings", HIDDEN: "Hidden"}
GLANCE = (RING, CHIP)

#: Most of each at the top of one tile. Three rings stacked is as tall as a
#: tile gets without scrolling; a board's sensors are the exception, since
#: its tile is nothing but chips and scrolls to take them all.
LIMITS = {RING: 3, CHIP: 8}
LIMITS_BY_ORDER = {40: {RING: DRIVE_RINGS, CHIP: 12}, 60: {RING: 3, CHIP: 50}}


def limits_for(order):
    return LIMITS_BY_ORDER.get(order, LIMITS)

#: Above this a temperature is worth colouring; above the second, worth alarm.
TEMP_WARN_C = 80.0
TEMP_HOT_C = 90.0

#: Groups that are detail by nature: dozens of per-core clocks and a board's
#: raw voltage rails are worth having, but not worth the top of a tile.
DETAIL_GROUPS = ("Clocks", "Voltages")

# Only groups whose readings measure the same thing. Averaging sixteen core
# clocks describes the CPU; averaging a board's eighteen voltage rails
# describes nothing, since 3.4 V and 0.7 V are different rails, not a range.
SUMMARY_GROUPS = ("Clocks",)

#: Devices folded into one tile, by Device.order: key and title.
#: A machine with five drives should not spend five tiles on one number each.
MERGED = {40: ("drives", "Drives")}

#: Most rows a tile shows before the rest moves behind the chevron.
BODY_MAX = 6

#: Readings in one detail group before it earns a summary row in the body.
SERIES_MIN = 3


def unit_for(kind):
    return UNITS.get(kind, "")


def format_value(value, kind, *, decimals=None):
    """A reading as text, unit included. MISSING when there is no number."""
    if value is None:
        return MISSING
    try:
        number = float(value)
    except (TypeError, ValueError):
        return MISSING
    if decimals is None:
        decimals = DECIMALS.get(kind, 1)
    return ("%.*f %s" % (decimals, number, unit_for(kind))).strip()


def format_pair(value, total, kind):
    """A reading against its maximum, as "12.4 / 32.0 GB"."""
    if value is None or not total:
        return format_value(value, kind)
    try:
        number, ceiling = float(value), float(total)
    except (TypeError, ValueError):
        return format_value(value, kind)
    if kind == "memory_gb":
        return _amount_pair(number, ceiling)
    decimals = DECIMALS.get(kind, 1)
    return ("%.*f / %.*f %s"
            % (decimals, number, decimals, ceiling, unit_for(kind))).strip()


def _amount_pair(used, total):
    """Gigabytes against a total, rounded to what the total deserves.

    A tenth of a gigabyte matters on a 16 GB card and is noise on a 1 TB
    drive, where "776.0 / 983.3 GB" is two numbers too long to read. Past a
    terabyte the total says TB, and so does the amount used once it gets
    there - "12 GB / 3.9 TB" rather than a flat "0.0 / 3.9 TB".
    """
    if total >= 1000:
        if used >= 1000:
            return "%.1f / %.1f TB" % (used / 1000, total / 1000)
        return "%.0f GB / %.1f TB" % (used, total / 1000)
    if total >= 100:
        return "%.0f / %.0f GB" % (used, total)
    return "%.1f / %.1f GB" % (used, total)


def short_label(label, kind=None):
    """The one or two words a tile row is given, instead of a phrase."""
    by_kind = _BY_KIND.get(label)
    if by_kind and kind in by_kind:
        return by_kind[kind]
    return _SHORT_LABELS.get(label, label)


def level_for(value, kind):
    """How alarming a reading is: None, "warn" or "hot".

    Temperature only. Load and wattage are high because the machine is
    working, which is not a problem to be coloured; heat is.
    """
    if kind != "temp" or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number >= TEMP_HOT_C:
        return "hot"
    if number >= TEMP_WARN_C:
        return "warn"
    return None


def fraction_of(value, total, kind):
    """How full a reading is, 0..1, or None when it has no ceiling.

    A percentage is its own ceiling, which is what lets a load reading draw a
    bar without the provider having to declare a total it does not have.
    """
    if value is None:
        return None
    try:
        number = float(value)
        if total:
            share = number / float(total)
        elif kind == "utilization":
            share = number / 100.0
        else:
            return None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return max(0.0, min(1.0, share))


@dataclass(frozen=True)
class Metric:
    """One render-ready row. Frozen, so a widget can skip an unchanged one."""

    uid: str
    key: str
    device: str
    label: str
    group: str
    kind: str
    value: Optional[float]
    text: str
    lo: str = MISSING
    hi: str = MISSING
    fraction: Optional[float] = None
    level: Optional[str] = None      # None, "warn" or "hot"
    # The range as numbers, for a view that formats it its own way: the tile
    # blanks lo/hi when they are equal, the tree shows them regardless.
    low: Optional[float] = None
    high: Optional[float] = None
    total: Optional[float] = None
    #: The recorded reading this row stands in for, when its own uid is one
    #: the tile made up - the CPU's Vcore, which is the board's reading.
    source: str = ""

    @property
    def missing(self):
        return self.value is None


@dataclass(frozen=True)
class TreeModel:
    """One device as the classic tree shows it: every reading, by group."""

    key: str
    name: str
    order: int
    groups: tuple = ()      # ((group, (Metric, ...)), ...)
    #: The provider's own name, whatever the user renamed it to: `name`
    #: differing from it is how a rename is told apart.
    device_name: str = ""


@dataclass(frozen=True)
class TileConfig:
    """What the user chose for one tile. The default is fully automatic.

    `custom` says the placements are the user's: until the first edit the
    tile keeps choosing for itself, so a sensor that appears later still
    finds its way onto it. A renamed reading or a colour of its own does not
    make the placements the user's: both sit on top of whichever placements
    are in force. Stored as JSON, keyed by the tile's key.
    """

    custom: bool = False
    rings: tuple = ()        # uids, in order
    chips: tuple = ()        # uids, in order
    below: tuple = ()        # uids, in order
    hidden: tuple = ()       # uids
    labels: tuple = ()       # ((uid, name), ...): readings the user renamed
    colour: str = ""         # "#rrggbb", or "" for the device's own

    def is_default(self):
        return self == TileConfig()

    def label(self, uid):
        return dict(self.labels).get(uid)

    def to_json(self):
        return json.dumps({"custom": self.custom, "rings": list(self.rings),
                           "chips": list(self.chips),
                           "below": list(self.below),
                           "hidden": list(self.hidden),
                           "labels": dict(self.labels),
                           "colour": self.colour})

    @classmethod
    def from_json(cls, text):
        """Tolerant: anything unreadable is the default, not an error."""
        try:
            data = json.loads(text)

            def uids(name, most=None):
                found = tuple(str(u) for u in data.get(name, ()))
                return found[:most] if most else found

            labels = data.get("labels") or {}
            colour = str(data.get("colour") or "")
            return cls(custom=bool(data.get("custom", False)),
                       rings=uids("rings"),
                       # "glance" is what the first version of the editor
                       # saved: its bold numbers are chips now.
                       chips=uids("chips") or uids("glance"),
                       below=uids("below"), hidden=uids("hidden"),
                       labels=tuple(sorted((str(u), str(n))
                                           for u, n in labels.items()
                                           if str(n).strip())),
                       colour=colour if _HEX_COLOUR.match(colour) else "")
        except (TypeError, ValueError, AttributeError):
            return cls()

    def placement(self, uid):
        for placement in (RING, CHIP, BELOW, HIDDEN):
            if uid in getattr(self, _FIELDS[placement]):
                return placement
        return FOLDED


_FIELDS = {RING: "rings", CHIP: "chips", BELOW: "below", HIDDEN: "hidden"}
_HEX_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


def rename(config, uid, name):
    """`config` with reading `uid` called `name` on this tile.

    A blank name, or the reading's own, gives it back its own name.
    """
    labels = dict(config.labels)
    name = (name or "").strip()
    if name:
        labels[uid] = name
    else:
        labels.pop(uid, None)
    return replace(config, labels=tuple(sorted(labels.items())))


def recolour(config, colour):
    """`config` drawn in `colour` ("#rrggbb"), or in its own with ""."""
    colour = colour if colour and _HEX_COLOUR.match(colour) else ""
    return replace(config, colour=colour.lower())


def ringable(metric):
    """Whether a reading has a top for a ring to fill up to."""
    return bool(metric.total) or metric.kind == "utilization"


def _snapshot(config, model):
    """The automatic choice written down, so the first edit starts from it.

    Locked rings are never written down: they are the tile's, not the
    user's, and are put back in front of whatever the user chose.
    """
    if config.custom:
        return config
    return replace(config, custom=True,
                   **{_FIELDS[p]: tuple(m.uid for q, m in model.items
                                        if q == p and m.uid not in model.locked)
                      for p in (RING, CHIP, BELOW)},
                   hidden=())


def can_place(config, model, uid, placement):
    """Whether `uid` may go to `placement` on this tile."""
    if uid in model.locked:
        return placement == RING
    config = _snapshot(config, model)
    if config.placement(uid) == placement:
        return True
    if placement == RING:
        metric = next((m for _p, m in model.items if m.uid == uid), None)
        if metric is None or not ringable(metric):
            return False
        return len(model.locked) + len(config.rings) < model.limit(RING)
    if placement == CHIP:
        return len(config.chips) < model.limit(CHIP)
    return True


def place(config, model, uid, placement):
    """`config` with `uid` moved to `placement`. Unchanged if it cannot go.

    A full row refuses one more rather than pushing the oldest out: losing a
    reading the user placed deliberately would be a surprise.
    """
    if uid in model.locked or not can_place(config, model, uid, placement):
        return config
    config = _snapshot(config, model)
    if config.placement(uid) == placement:
        return config
    lists = {p: [u for u in getattr(config, name) if u != uid]
             for p, name in _FIELDS.items()}
    if placement in lists:
        lists[placement].append(uid)
    return replace(config, **{_FIELDS[p]: tuple(uids)
                              for p, uids in lists.items()})


def move(config, model, uid, delta):
    """`config` with `uid` shifted `delta` places within its own list."""
    if uid in model.locked:
        return config
    config = _snapshot(config, model)
    for name in ("rings", "chips", "below"):
        items = list(getattr(config, name))
        if uid in items:
            index = items.index(uid)
            target = max(0, min(len(items) - 1, index + delta))
            items.insert(target, items.pop(index))
            return replace(config, **{name: tuple(items)})
    return config


def reset(config):
    """Back to the default tile: automatic placement, the readings' own
    names and the device's own colour."""
    return TileConfig()


def ring_fraction(metric):
    """How full a ring is for this reading, or None for an empty track.

    Only a share or an amount of a total has a top; a ring is offered for
    nothing else, and a reading that has stopped reporting shows the track.
    """
    if metric.value is None:
        return None
    return metric.fraction


@dataclass(frozen=True)
class TileModel:
    """Everything one device tile draws."""

    key: str
    name: str
    order: int
    rings: tuple = ()
    chips: tuple = ()
    rows: tuple = ()
    detail: tuple = ()      # ((group, (Metric, ...)), ...)
    #: Every reading the editor lists, with where it sits:
    #: ((placement, Metric), ...), each placement in its shown order.
    items: tuple = ()
    config: TileConfig = TileConfig()
    #: Uids of the rings that are part of the tile and cannot be moved.
    locked: tuple = ()
    #: The user's colour for this tile, "#rrggbb"; "" for the device's.
    colour: str = ""
    #: The readings' own names, by uid, for the ones the user renamed: what
    #: a cleared name goes back to.
    original_labels: tuple = ()

    @property
    def rings_in_a_row(self):
        """Drives lay their rings side by side, with the chips under them:
        a volume and a drive's temperature do not pair up the way a card's
        load and its hot spot do."""
        return self.order == 40

    @property
    def hottest(self):
        """The reading behind the tile's level: the hottest alarming one."""
        worst = None
        groups = [self.headline, self.rows]
        groups += [metrics for _group, metrics in self.detail]
        for metrics in groups:
            for metric in metrics:
                if metric.level is None or metric.value is None:
                    continue
                if worst is None or metric.value > worst.value:
                    worst = metric
        return worst

    def limit(self, placement):
        return limits_for(self.order).get(placement)

    @property
    def headline(self):
        """Everything at the top of the tile, rings first."""
        return self.rings + self.chips

    @property
    def level(self):
        """The most alarming reading anywhere on this tile."""
        worst = None
        groups = [self.headline, self.rows]
        groups += [metrics for _group, metrics in self.detail]
        for metrics in groups:
            for metric in metrics:
                if metric.level == "hot":
                    return "hot"
                if metric.level == "warn":
                    worst = "warn"
        return worst

    @property
    def headline_only(self):
        """Nothing below the headline; the tile should render compact.

        A drive that reports one temperature has a complete tile in its
        headline alone. Repeating that reading underneath to fill the space
        would only print the same number twice.
        """
        return not self.rows and not self.detail

    @property
    def empty(self):
        return self.headline_only and not any(
            m.value is not None for m in self.headline)


class History:
    """The lowest and highest each sensor has read since the app started."""

    def __init__(self):
        self._bounds = {}

    def update(self, uid, value):
        if value is None:
            return self._bounds.get(uid)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return self._bounds.get(uid)
        low, high = self._bounds.get(uid, (number, number))
        self._bounds[uid] = (min(low, number), max(high, number))
        return self._bounds[uid]

    def bounds(self, uid):
        return self._bounds.get(uid)

    def forget(self, uid):
        self._bounds.pop(uid, None)

    def clear(self):
        self._bounds.clear()


class Staleness:
    """Which sensors have stopped reporting for long enough to be blanked.

    A drive that was unplugged or a module that was unloaded otherwise leaves
    its last value on screen looking live. One dropped reading is not enough:
    a single missed tick would make the whole window flicker.
    """

    GRACE = 3

    def __init__(self, grace=None):
        self.grace = self.GRACE if grace is None else grace
        self._misses = {}

    def mark(self, seen, known):
        """Count one tick. Returns the uids that are now stale."""
        stale = set()
        for uid in known:
            if uid in seen:
                self._misses.pop(uid, None)
                continue
            misses = self._misses.get(uid, 0) + 1
            self._misses[uid] = misses
            if misses >= self.grace:
                stale.add(uid)
        return stale

    def forget(self, uid):
        self._misses.pop(uid, None)

    def clear(self):
        self._misses.clear()


def _group_rank(group):
    return GROUP_ORDER.index(group) if group in GROUP_ORDER else len(GROUP_ORDER)


def _blanked(metric):
    """The same row with its value gone, keeping the bounds it did reach."""
    if metric.value is None:
        return metric
    return replace(metric, value=None, text=MISSING, fraction=None,
                   level=None)


#: Key prefix of a headline slot waiting for its reading. Distinct from a
#: summary's plain "!": a summary is a real reading that can be placed, a
#: placeholder is only a hole.
PLACEHOLDER = "!slot-"


def _placeholder(device, kind):
    """A headline slot that is expected but has not reported yet.

    Held open rather than left out: the CPU package wattage comes from a delta
    counter that reports nothing on the first tick, and a slot that appears a
    second after the window opens relayouts the tile under the cursor.
    """
    return Metric(uid="%s/%s%s" % (device, PLACEHOLDER, kind),
                  key="%s%s" % (PLACEHOLDER, kind), device=device,
                  label=SLOT_CAPTIONS.get(kind, ""), group="", kind=kind,
                  value=None, text=MISSING)


def _is_placeholder(metric):
    return metric.key.startswith(PLACEHOLDER)


def graph_uid(metric):
    """The uid Live Graphs has a history for behind this row, or None.

    A made-up row - a slot still waiting for its reading, or a summary of
    thirty-two core clocks - has a key starting "!" and no history of its
    own; one standing in for a real reading graphs that reading.
    """
    if metric is None:
        return None
    if metric.source:
        return metric.source
    return None if metric.key.startswith("!") else metric.uid


def _with_items(tile, extra=()):
    """The automatic tile, with every reading listed where it sits.

    Placeholders are left out: they hold a slot open for a reading that has
    not arrived, and there is nothing to place until it does.
    """
    items = [(RING, m) for m in tile.rings if not _is_placeholder(m)]
    items += [(CHIP, m) for m in tile.chips if not _is_placeholder(m)]
    items += [(BELOW, m) for m in tile.rows]
    items += [(FOLDED, m) for _group, metrics in tile.detail for m in metrics]
    seen = {m.uid for _p, m in items}
    items += [(FOLDED, m) for m in extra if m.uid not in seen]
    return replace(tile, items=tuple(items))


def _custom_tile(tile, config):
    """A tile laid out exactly as the user placed its readings.

    Readings are looked up in what the automatic tile listed, so a merged
    tile's drive names and the derived readings come along. One placed but
    not reporting right now keeps its slot as a dash rather than reshuffling
    the tile; one that has never been seen is simply not shown.
    """
    by_uid = {}
    groups = {}
    for placement, metric in tile.items:
        by_uid[metric.uid] = metric
    for group, metrics in tile.detail:
        for metric in metrics:
            groups[metric.uid] = group
    for metric in tile.rows + tile.headline:
        groups.setdefault(metric.uid, metric.group or "More")

    def picked(uids, test=lambda m: True):
        return tuple(by_uid[u] for u in uids if u in by_uid
                     and u not in tile.locked and test(by_uid[u]))

    locked = tuple(m for m in tile.rings if m.uid in tile.locked)
    rings = (locked + picked(config.rings, ringable))[:tile.limit(RING)]
    chips = picked(config.chips)[:tile.limit(CHIP)]
    rows = picked(config.below)
    placed = (set(config.rings) | set(config.chips) | set(config.below)
              | set(config.hidden) | set(tile.locked))

    detail = {}
    for _placement, metric in tile.items:
        if metric.uid in placed or metric.key.startswith("!"):
            continue                # derived readings show only where placed
        detail.setdefault(groups.get(metric.uid, metric.group or "More"),
                          []).append(metric)
    ordered = tuple((group, tuple(detail[group]))
                    for group in sorted(detail, key=_group_rank))

    items = [(RING, m) for m in rings] + [(CHIP, m) for m in chips]
    items += [(BELOW, m) for m in rows]
    items += [(FOLDED, m) for _g, metrics in ordered for m in metrics]
    items += [(FOLDED, m) for _p, m in tile.items
              if m.key.startswith("!") and m.uid not in placed]
    items += [(HIDDEN, by_uid[u]) for u in config.hidden
              if u in by_uid and u not in tile.locked]
    return replace(tile, rings=rings, chips=chips, rows=rows, detail=ordered,
                   items=tuple(items), config=config)


#: What an averaged group is called on a tile.
SUMMARY_LABELS = {"Clocks": "Avg clock"}


def _renamed(tile, labels):
    """The tile with the user's names on the readings they renamed."""
    originals = {}

    def named(metric):
        name = labels.get(metric.uid)
        if not name or name == metric.label:
            return metric
        originals[metric.uid] = metric.label
        return replace(metric, label=name)

    def each(metrics):
        return tuple(named(m) for m in metrics)

    return replace(
        tile, rings=each(tile.rings), chips=each(tile.chips),
        rows=each(tile.rows),
        detail=tuple((group, each(ms)) for group, ms in tile.detail),
        items=tuple((p, named(m)) for p, m in tile.items),
        original_labels=tuple(sorted(originals.items())))


def collapse_series(metrics, label):
    """Fold a long run of like readings into one row: average, low, high.

    Thirty-two core clocks are worth keeping and not worth reading one by one,
    so the body gets the shape of them and the chevron keeps the detail.
    """
    values = [m.value for m in metrics if m.value is not None]
    kind = metrics[0].kind
    device = metrics[0].device
    name = SUMMARY_LABELS.get(label, "%s (avg)" % label)
    if not values:
        return Metric(uid="%s/!%s" % (device, label), key="!%s" % label,
                      device=device, label=name, group=metrics[0].group,
                      kind=kind, value=None, text=MISSING)
    average = sum(values) / len(values)
    return Metric(uid="%s/!%s" % (device, label), key="!%s" % label,
                  device=device, label=name, group=metrics[0].group, kind=kind,
                  value=average, text=format_value(average, kind),
                  lo=format_value(min(values), kind),
                  hi=format_value(max(values), kind))


def hottest(metrics, device):
    """The hottest of a CPU's temperatures, as a reading of its own.

    What "hot spot" means on a card, for a processor: the highest of every
    sensor on the die, whichever it is this second. Tctl is left out where
    the chip also reports Tdie - k10temp publishes Tdie only when Tctl
    carries a fan-curve offset (20 °C on some first-generation Ryzens), and
    there Tctl is not a temperature of anything.
    """
    temps = [m for m in metrics if m.kind == "temp" and m.key[:1] != "!"]
    if any(m.key == "Tdie" for m in temps):
        temps = [m for m in temps if m.key != "Tctl"]
    if not temps:
        return None
    live = [m for m in temps if m.value is not None]
    uid = "%s/!hotspot" % device
    if not live:
        return Metric(uid=uid, key="!hotspot", device=device, label="Hot Spot",
                      group="Temperatures", kind="temp", value=None,
                      text=MISSING)
    top = max(live, key=lambda m: m.value)
    return Metric(uid=uid, key="!hotspot", device=device, label="Hot Spot",
                  group="Temperatures", kind="temp", value=top.value,
                  text=top.text, level=top.level)


def _pick(metrics, kind, preferred, used, exact=False):
    """The reading that best fills a headline slot, or None.

    Exact keys first, then a loose match: a super-I/O chip names its sensors
    inside the label rather than the key, so SYSTIN arrives as "NCT6799
    SYSTIN" and would never match on equality. A derived reading ("!...")
    is only ever taken by name: it stands for its group, not beside it.
    """
    candidates = [m for m in metrics if m.kind == kind and m.uid not in used]
    for want in preferred:
        for metric in candidates:
            if metric.key == want:
                return metric
    candidates = [m for m in candidates if m.key[:1] != "!"]
    if exact or not candidates:
        return None
    if kind == "fan":
        # A header with nothing plugged in reads 0 and is not "the fan".
        turning = [m for m in candidates if m.value]
        candidates = turning or candidates
    for want in preferred:
        needle = want.lower()
        for metric in candidates:
            if needle in metric.key.lower() or needle in metric.label.lower():
                return metric
    return candidates[0]


def _natural(text):
    """Sort key that puts DIMM 2 before DIMM 10."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)]


def _fill(device, pool, slots, used, most, rank=None, locked=None):
    """The readings for one row of slots, marking each one taken.

    Uids filled by a locked slot are added to `locked` when it is given.
    """
    out = []
    for slot in slots:
        if slot.many == EVERY:
            picks = sorted((m for m in pool if m.kind == slot.kind
                            and m.uid not in used and m.key[:1] != "!"),
                           key=rank or (lambda m: _natural(m.label)))
        else:
            pick = _pick(pool, slot.kind, slot.keys, used, exact=slot.locked)
            if pick is None and slot.many == WAIT:
                pick = _placeholder(device, slot.kind)
            picks = [pick] if pick is not None else []
        for metric in picks:
            if len(out) < most:
                used.add(metric.uid)
                out.append(metric)
                if slot.locked and locked is not None:
                    locked.append(metric.uid)
    return out


#: A super-I/O chip's model at the front of a label: "NCT6799 SYSTIN".
_CHIP_NAME = re.compile(r"^[A-Z]{2,}\d{3,}[A-Z]*\s+(?=\S)")


def _without_chip_names(metrics):
    """Labels without the chip model in front, where that stays unambiguous.

    On a board's tile every sensor says "NCT6799", which is a chip nobody
    looks for and half a chip's width. Kept where dropping it would give
    two readings of one kind the same name - a board with two monitoring
    chips, both with a SYSTIN.
    """
    stripped = [_CHIP_NAME.sub("", m.label) for m in metrics]
    names = {}
    for metric, label in zip(metrics, stripped):
        names.setdefault((metric.kind, label), []).append(metric.uid)
    return [replace(m, label=label)
            if label != m.label and len(names[(m.kind, label)]) == 1 else m
            for m, label in zip(metrics, stripped)]


def board_vcore(metrics):
    """The CPU's core voltage as the board measures it, or None.

    Only where a driver or lm-sensors has named the rail Vcore: which rail
    is the CPU's is a fact about the chip's pinout, and an unnamed "Rail 0"
    is not claimed for the CPU on a guess.
    """
    for metric in metrics:
        if metric.kind == "voltage" and re.match(r"^(cpu\s*)?vcore",
                                                 metric.label, re.I):
            return replace(metric, uid="cpu/!vcore", key="!vcore",
                           device="cpu", label="Voltage", group="Voltages",
                           source=metric.uid)
    return None


def plausible(metric):
    """A temperature a sensor could really be reading."""
    low, high = PLAUSIBLE_C
    return metric.value is not None and low <= metric.value < high


def _volume_rank(metric):
    """Boot loader volumes after real ones, then by mount point."""
    mount = metric.key[len("vol:"):]
    boot = any(mount == m or mount.startswith(m + "/") for m in BOOT_MOUNTS)
    return (boot, mount != "/", mount)


def drive_order(key):
    """Drives from 0 to N: nvme0n1, nvme1n1, sda, sdb..."""
    return _natural(key.split(":", 1)[-1])


class TileBuilder:
    """Turns each tick's flat readings into one model per device.

    Two renders of the same state: build()/rebuild() give the tiles, tree()
    the classic device > group > reading listing. Stateful on purpose:
    min/max, the grace period for a sensor that stops reporting, and the
    last value of one that has are all things a single sample cannot know.
    """

    def __init__(self, devices=None, *, grace=None):
        self.devices = dict(devices or {})
        self.history = History()
        self.staleness = Staleness(grace)
        # The readings themselves rather than the rows built from them, so
        # that a rename or a cleared range can be re-rendered without waiting
        # for the next tick and without inventing values.
        self._readings = {}      # uid -> Reading, last seen
        self._stale = set()      # uids that have stopped reporting
        self._order = {}         # uid -> first-seen index, to keep rows steady
        # A board's hottest sensors when the app started, by tile key: its
        # default chips, kept rather than re-ranked on every tick so they do
        # not trade places as two readings cross.
        self._pinned = {}
        #: The user's per-tile choices, by tile key. Set by the window; read
        #: at every build.
        self.configs = {}

    def known(self):
        """Whether any reading has arrived yet."""
        return bool(self._readings)

    def clear_bounds(self):
        self.history.clear()

    def forget(self):
        """Drop everything remembered; used when the device list changes."""
        self._readings.clear()
        self._stale.clear()
        self._order.clear()
        self._pinned.clear()
        self.staleness.clear()

    def _metric(self, reading):
        bounds = self.history.update(reading.uid, reading.value)
        low = high = MISSING
        if reading.total:
            # A reading with a ceiling has its maximum stated in the value
            # itself, so repeating the run of it would say nothing new.
            text = format_pair(reading.value, reading.total, reading.kind)
        else:
            text = format_value(reading.value, reading.kind)
            if bounds:
                low = format_value(bounds[0], reading.kind)
                high = format_value(bounds[1], reading.kind)
                # A sensor that has not moved since the window opened reads
                # "49.1 °C  49.1 °C / 49.1 °C", which is the same number three
                # times. The range earns its place once there is one.
                if low == high:
                    low = high = MISSING
        return Metric(
            uid=reading.uid,
            key=reading.key,
            device=reading.device,
            label=reading.label,
            group=reading.group,
            kind=reading.kind,
            value=reading.value,
            text=text,
            lo=low,
            hi=high,
            fraction=fraction_of(reading.value, reading.total, reading.kind),
            level=level_for(reading.value, reading.kind),
            low=bounds[0] if bounds else None,
            high=bounds[1] if bounds else None,
            total=reading.total or None,
        )

    def build(self, readings, *, names=None, group_visible=None):
        """One tick's readings as a tile per device, in display order."""
        seen = set()
        for reading in readings:
            seen.add(reading.uid)
            if reading.uid not in self._order:
                self._order[reading.uid] = len(self._order)
            self._readings[reading.uid] = reading

        # Marked from every reading, before visibility filtering: a group the
        # user has hidden is not a sensor that has stopped reporting, and
        # unhiding it should not show three seconds of dashes.
        self._stale = self.staleness.mark(seen, list(self._readings))
        return self.rebuild(names=names, group_visible=group_visible)

    def rebuild(self, *, names=None, group_visible=None):
        """The tiles again from the readings in hand, without a new tick.

        What a tile is called and which groups it shows are the user's to
        change at any moment; waiting a second for the next sample to show
        the change would make the window feel broken.
        """
        names = names or {}
        # A tile row gets a word or two where the tree gets the whole phrase:
        # "Memory Used" next to "9.8 / 32.0 GB" says "used" twice.
        grouped = {
            device: _without_chip_names(
                [replace(m, label=short_label(m.label, m.kind))
                 for m in metrics])
            for device, metrics in self._grouped(group_visible).items()}

        # The board's Vcore is the CPU's core voltage; the CPU tile borrows
        # it where the processor reports none of its own.
        vcore = None
        if not any(m.kind == "voltage" for m in grouped.get("cpu", ())):
            vcore = board_vcore(m for key, metrics in grouped.items()
                                if key != "cpu" for m in metrics)

        tiles = []
        done = set()
        for key in self._device_order(grouped):
            device = self.devices.get(key)
            order = device.order if device else 50
            members = self._merge_members(order)
            if members is not None:
                merge_key, title = MERGED[order]
                if merge_key in done:
                    continue
                done.add(merge_key)
                tile = self._drive_tile(
                    key=merge_key,
                    name=names.get(merge_key) or title,
                    order=order,
                    members=[(k, names.get(k) or self.devices[k].name,
                              grouped.get(k, [])) for k in members],
                    merged=True,
                )
            elif order == 40:
                tile = self._drive_tile(
                    key=key,
                    name=names.get(key) or (device.name if device else key),
                    order=order,
                    members=[(key, "", grouped.get(key, []))],
                    merged=False,
                )
            elif key == "cpu" and vcore is not None:
                tile = self._tile(
                    key=key,
                    name=names.get(key) or (device.name if device else key),
                    order=order,
                    metrics=grouped.get(key, []),
                    borrowed=(vcore,),
                )
            else:
                tile = self._tile(
                    key=key,
                    name=names.get(key) or (device.name if device else key),
                    order=order,
                    metrics=grouped.get(key, []),
                )
            tiles.append(self._configured(tile))
        return tiles

    def _configured(self, tile):
        """The tile with its user's choices applied on top of the automatic one.

        The automatic tile is always built first: it is what the editor offers
        as the starting point, and the one the tile falls back to on reset.
        """
        config = self.configs.get(tile.key, TileConfig())
        if config.custom:
            tile = _custom_tile(tile, config)
        else:
            tile = replace(tile, config=config)
        tile = replace(tile, colour=config.colour)
        return _renamed(tile, dict(config.labels)) if config.labels else tile

    def tree(self, *, names=None, group_visible=None):
        """The same readings as the classic tree shows them.

        No merging, no headline, no summaries: every drive is its own
        category and every core clock its own row, which is what a tree
        with a column per number is for.
        """
        names = names or {}
        grouped = self._grouped(group_visible)
        models = []
        for key in self._device_order(grouped):
            device = self.devices.get(key)
            groups = {}
            for metric in grouped.get(key, []):
                groups.setdefault(metric.group, []).append(metric)
            models.append(TreeModel(
                key=key,
                name=names.get(key) or (device.name if device else key),
                order=device.order if device else 50,
                groups=tuple((group, tuple(groups[group]))
                             for group in sorted(groups, key=_group_rank)),
                device_name=device.name if device else key,
            ))
        return models

    def _grouped(self, group_visible):
        """This tick's rows by device, stale ones blanked, hidden ones out."""
        grouped = {}
        for uid, reading in self._readings.items():
            metric = self._metric(reading)
            if uid in self._stale:
                metric = _blanked(metric)
            if group_visible is not None and metric.group:
                if not group_visible.get(metric.group, True):
                    continue
            grouped.setdefault(metric.device, []).append(metric)

        for metrics in grouped.values():
            metrics.sort(key=lambda m: (_group_rank(m.group),
                                        self._order.get(m.uid, 0)))
        return grouped

    def _merge_members(self, order):
        """The devices folded into one tile at this order, or None.

        A single drive keeps its own tile and its own name: the merge earns
        its place only where the alternative is a row of near-empty tiles.
        """
        if order not in MERGED:
            return None
        members = [d.key for d in sorted(self.devices.values(),
                                         key=lambda d: drive_order(d.key))
                   if d.order == order]
        return members if len(members) > 1 else None

    def _drive_tile(self, key, name, order, members, merged):
        """One drive's tile, or one tile for several.

        Rings for the volumes - the one the system runs from first, then the
        rest of that drive, then the other drives' - and a temperature chip
        per drive, from the first drive to the last. On a tile of several a
        chip is captioned with its drive's name: "Composite" alone does not
        say whose it is. Everything else a drive reports goes into the fold.
        """
        layout = LAYOUTS.get(order, DEFAULT_LAYOUT)
        limits = limits_for(order)
        system = next((k for k, _n, metrics in members
                       if any(m.key == "vol:/" for m in metrics)), None)
        volumes, chips, rows, detail = [], [], [], {}
        for index, (member_key, label, metrics) in enumerate(members):
            used = set()
            mine = sorted((m for m in metrics if m.key.startswith("vol:")),
                          key=_volume_rank)
            # The boot loader's volumes last of all, then the system drive
            # ahead of the others, each drive's in its own order.
            volumes += [((_volume_rank(m)[0], member_key != system, index,
                          position), m) for position, m in enumerate(mine)]
            used |= {m.uid for m in mine}
            for metric in _fill(member_key, metrics, layout.chips, used, 1):
                chips.append(replace(metric, label=label) if merged
                             else metric)
            for metric in metrics:
                if metric.uid in used:
                    continue
                if merged:
                    detail.setdefault(label, []).append(replace(
                        metric, label="%s · %s" % (label, metric.label)))
                else:
                    rows.append(metric)

        volumes = [m for _rank, m in sorted(volumes, key=lambda v: v[0])]
        rings = volumes[:limits[RING]]
        rows = volumes[limits[RING]:] + rows
        rows += chips[limits[CHIP]:]
        chips = chips[:limits[CHIP]]
        for metric in rows[BODY_MAX:]:
            detail.setdefault(metric.group or "More", []).append(metric)
        ordered = tuple((group, tuple(detail[group]))
                        for group in sorted(detail, key=_group_rank))
        return _with_items(TileModel(key=key, name=name, order=order,
                                     rings=tuple(rings), chips=tuple(chips),
                                     rows=tuple(rows[:BODY_MAX]),
                                     detail=ordered))

    def _board_chips(self, key, metrics, used):
        """A board's hottest sensors when the app started, hottest first."""
        if key not in self._pinned:
            temps = sorted((m for m in metrics
                            if m.kind == "temp" and plausible(m)),
                           key=lambda m: -m.value)
            if not temps:
                return []
            self._pinned[key] = tuple(m.uid for m in temps[:BOARD_CHIPS])
        by_uid = {m.uid: m for m in metrics}
        chips = [by_uid[u] for u in self._pinned[key] if u in by_uid]
        used |= {m.uid for m in chips}
        return chips

    def _device_order(self, grouped):
        """Known devices in their declared order, then any stragglers."""
        known = sorted(self.devices.values(), key=lambda d: (d.order, d.name))
        keys = [d.key for d in known]
        keys += sorted(k for k in grouped if k not in self.devices)
        return keys

    def _tile(self, key, name, order, metrics, borrowed=()):
        layout = LAYOUTS.get(order, DEFAULT_LAYOUT)
        limits = limits_for(order)

        # Readings a tile can lead with that no sensor reports as such: the
        # average of a run of clocks, and on a CPU its hottest spot and the
        # core voltage borrowed from the board.
        series = {}
        for metric in metrics:
            if metric.group in SUMMARY_GROUPS:
                series.setdefault(metric.group, []).append(metric)
        derived = [collapse_series(items, group)
                   for group, items in series.items()
                   if len(items) >= SERIES_MIN]
        if order == 10:
            spot = hottest(metrics, key)
            if spot is not None:
                derived.insert(0, spot)
        derived += list(borrowed)

        pool = list(metrics) + derived
        used, locked = set(), []
        rings = _fill(key, pool, layout.rings, used, limits[RING],
                      locked=locked)
        if order == 60:
            chips = self._board_chips(key, metrics, used)
        else:
            chips = _fill(key, pool, layout.chips, used, limits[CHIP],
                          rank=layout.rank)

        body, detail = [], {}
        for metric in metrics:
            if metric.uid in used:
                continue
            if metric.group in DETAIL_GROUPS:
                detail.setdefault(metric.group, []).append(metric)
            else:
                body.append(metric)
        for metric in body[BODY_MAX:]:
            detail.setdefault(metric.group or "More", []).append(metric)
        body = body[:BODY_MAX]

        ordered = tuple((group, tuple(detail[group]))
                        for group in sorted(detail, key=_group_rank))
        # Derived readings not leading the tile are still offered to the
        # editor, so the average of the clocks can be placed like any other.
        extra = tuple(m for m in derived if m.uid not in used)
        return _with_items(TileModel(key=key, name=name, order=order,
                                     rings=tuple(rings), chips=tuple(chips),
                                     rows=tuple(body), detail=ordered,
                                     locked=tuple(locked)), extra)
