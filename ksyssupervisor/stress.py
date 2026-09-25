"""Stress testing: putting deliberate load on the CPU, memory and GPU.

The unprivileged, Qt-free half, following the same rule as the sensor providers
and fanctl: the logic is testable against fake sysfs trees without a display.

Every load runs in its own process rather than a thread. For the CPU that is
forced by the GIL - Python threads would contend for one interpreter instead of
saturating cores. For the GPU it is a safety property: a driver that hangs takes
a child process with it and not the window, and graphics memory is only
reliably returned when the process that allocated it exits.
"""

from __future__ import annotations

import glob
import multiprocessing as mp
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import log
from .hardware import gpu_cards
from .hwmon import run_cmd

CPU_ROOT = "/sys/devices/system/cpu"

#: Length of one busy/idle cycle. Short enough that the load feels steady rather
#: than pulsing, long enough that sleep() overhead is not most of the slice.
SLICE_SECONDS = 0.05

#: The correction loop's step. Deliberately gentle: the measurement is a
#: one-second average, so reacting hard to it oscillates.
CORRECTION_GAIN = 0.5

#: Memory is committed in blocks rather than one allocation, so a request that
#: turns out to be too large fails partway with most of it already reported
#: instead of raising and reporting nothing.
RAM_BLOCK = 64 * 1024 * 1024

PAGE = 4096

#: Marks a worker as the kernel's preferred victim. A stress test that provokes
#: an out-of-memory condition should be what dies, not the user's session.
OOM_ADJUST = 1000


# ---------------------------------------------------------------------------
# CPU topology
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CpuTopology:
    """Which logical CPUs exist and how they share physical cores."""

    logical: List[int] = field(default_factory=list)
    #: One logical CPU per physical core - the first of each sibling group.
    physical: List[int] = field(default_factory=list)

    @property
    def has_smt(self):
        return len(self.physical) < len(self.logical)

    def selection(self, count, physical_only):
        """The CPUs to load, given how many are wanted.

        Drawn from the physical list first even when threads are allowed, so
        asking for four workers on an eight-thread quad-core spreads them over
        four cores rather than doubling up on two.
        """
        pool = self.physical if physical_only else _interleave(self.physical,
                                                               self.logical)
        if not pool:
            return []
        return pool[:max(1, min(count, len(pool)))]


def _interleave(physical, logical):
    """Physical cores first, then their siblings.

    An Intel P/E-core part has no siblings on its efficiency cores, so this
    cannot assume a fixed stride; it just puts one-per-core ahead of the rest.
    """
    rest = [cpu for cpu in logical if cpu not in physical]
    return list(physical) + rest


def cpu_topology(root=CPU_ROOT):
    """Read the CPU layout from sysfs, falling back to a flat list.

    Derived from thread_siblings_list rather than assuming a stride, because
    the arrangement genuinely differs: this AMD part pairs (n, n+8), many Intel
    parts pair (2n, 2n+1), Intel P/E designs give the efficiency cores no
    sibling at all, and most ARM cores have none. Reading what the kernel says
    handles all of them without a vendor check.

    `root` is a parameter so the tests can point this at a fake tree.
    """
    logical = []
    groups = {}

    for path in glob.glob(os.path.join(root, "cpu[0-9]*")):
        name = os.path.basename(path)
        if not re.match(r"^cpu[0-9]+$", name):
            continue                    # cpuidle, cpufreq, ...
        index = int(name[3:])

        # An offline CPU cannot be loaded, and scheduling onto it fails.
        online = os.path.join(path, "online")
        if os.path.isfile(online) and _read_text(online) == "0":
            continue
        logical.append(index)

        siblings = _read_text(os.path.join(path, "topology",
                                           "thread_siblings_list"))
        key = siblings if siblings else str(index)
        groups.setdefault(key, []).append(index)

    logical.sort()
    physical = sorted(min(members) for members in groups.values())

    if not logical:
        # No sysfs topology at all: still usable, just without the physical
        # distinction. Better than refusing to run.
        try:
            import psutil
            logical = list(range(psutil.cpu_count(logical=True) or 1))
        except Exception:
            logical = [0]
        physical = list(logical)

    return CpuTopology(logical=logical, physical=physical)


def _read_text(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_int(path):
    text = _read_text(path)
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# GPU capability
# ---------------------------------------------------------------------------

@dataclass
class GpuTarget:
    """A card that can be loaded, and what can be measured about it."""

    key: str
    name: str
    driver: str
    path: str = ""                  # /sys/class/drm/cardN/device
    nvidia_index: Optional[int] = None
    vram_total: Optional[int] = None

    @property
    def measurable(self):
        """Whether utilisation can be read back, closing the control loop.

        Where it cannot, the load is still applied - it is only the reported
        percentage that becomes a request rather than a measurement, and the
        UI says so instead of showing a number it did not verify.
        """
        return self.busy_percent() is not None

    def busy_percent(self):
        """Current utilisation, 0-100, or None where the driver does not say."""
        if self.nvidia_index is not None:
            return _nvidia_field(self.nvidia_index, "utilization.gpu")
        # amdgpu and radeon publish this directly; i915/xe do not.
        return _read_int(os.path.join(self.path, "gpu_busy_percent"))

    def vram_used(self):
        """Graphics memory in use, in bytes, or None."""
        if self.nvidia_index is not None:
            mib = _nvidia_field(self.nvidia_index, "memory.used")
            return None if mib is None else mib * 1024 * 1024
        return _read_int(os.path.join(self.path, "mem_info_vram_used"))


def _nvidia_field(index, field_name):
    """One numeric field from nvidia-smi for one card."""
    out = run_cmd(["nvidia-smi", "--id=%d" % index,
                   "--query-gpu=%s" % field_name,
                   "--format=csv,noheader,nounits"])
    if not out:
        return None
    try:
        return int(float(out.strip().splitlines()[0]))
    except (ValueError, IndexError):
        return None


def gpu_targets(cards=None):
    """Every GPU that can be stressed, across vendors.

    Cards come from the same discovery the sensor providers use, so a machine
    with several is handled the same way here as everywhere else in the app.
    NVIDIA cards are matched to an nvidia-smi index so utilisation and memory
    can still be read where sysfs does not publish them.
    """
    targets = []
    nvidia = _nvidia_indexes()

    for card in (gpu_cards() if cards is None else cards):
        index = None
        if card.driver in ("nvidia", "nvidia-drm", "nouveau"):
            index = nvidia.get(card.pci)

        targets.append(GpuTarget(
            key=card.key,
            name=card.name,
            driver=card.driver,
            path=card.path,
            nvidia_index=index,
            vram_total=_read_int(os.path.join(card.path,
                                              "mem_info_vram_total")),
        ))
    return targets


def _nvidia_indexes():
    """Map PCI address -> nvidia-smi index, empty when there is no NVIDIA card."""
    out = run_cmd(["nvidia-smi", "--query-gpu=index,pci.bus_id",
                   "--format=csv,noheader"])
    if not out:
        return {}

    found = {}
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        # nvidia-smi prints 00000000:03:00.0; sysfs uses 0000:03:00.0.
        bus = parts[1].lower()
        if len(bus) > 12:
            bus = bus[4:]
        found[bus] = index
    return found


# ---------------------------------------------------------------------------
# GPU workloads
# ---------------------------------------------------------------------------

#: The kinds of load the graphics card can be put under: key, the label the
#: window shows, and what it actually does.
#:
#: The table lives here rather than beside the shaders it names because this
#: module is the Qt-free one. stressgpu.py is imported only inside the child
#: process - importing it here to read a label would pull Qt's GL stack into
#: the parent, which is the one thing that arrangement exists to avoid. The
#: shader sources stay there and are keyed by these strings; a test checks the
#: two halves still agree.
#:
#: Keys are plain strings because every worker is started with the "spawn"
#: context, so anything handed to one has to pickle.
GPU_MODES = [
    ("transcendental", "Transcendental",
     "Sine, cosine and square roots in a chain where each result feeds the "
     "next. That dependency is the point: the card is limited by how quickly "
     "one result can follow another rather than by how much it can do at "
     "once, so the load lands on the special-function units and stays very "
     "steady. This is what earlier versions of this test always ran."),
    ("fma", "Arithmetic throughput",
     "Many independent multiply-and-add streams at once - the operation "
     "graphics and machine-learning work is mostly made of. Because the "
     "streams do not wait on each other, the shader cores stay saturated, "
     "which usually makes this the hottest of the four and the one to choose "
     "when looking for a cooling or power limit."),
    ("memory", "Memory bandwidth",
     "Reads scattered across a large texture, far enough apart to miss the "
     "cache, so the card spends its time waiting on memory rather than "
     "calculating. This stresses the memory controllers and the VRAM modules, "
     "which have thermal limits of their own and are often the first thing to "
     "misbehave on a card that is unstable under load."),
    ("combined", "Combined",
     "Alternates between the arithmetic and the memory loads. Neither is as "
     "extreme as it would be on its own, but the mixture is much closer to "
     "what a game or a real workload actually asks of the card."),
]

DEFAULT_GPU_MODE = GPU_MODES[0][0]


def gpu_mode_keys():
    return [key for key, _label, _text in GPU_MODES]


def gpu_mode_label(key):
    """The label for a key, or the key itself if it is not one we know."""
    for mode, label, _text in GPU_MODES:
        if mode == key:
            return label
    return key


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class StressConfig:
    """What the user asked for. Validated before anything is started."""

    cpu_enabled: bool = False
    cpu_workers: int = 1
    cpu_physical_only: bool = True
    cpu_percent: int = 100

    ram_enabled: bool = False
    ram_bytes: int = 0

    gpu_enabled: bool = False
    gpu_key: str = ""
    gpu_percent: int = 100
    gpu_mode: str = DEFAULT_GPU_MODE
    vram_bytes: int = 0

    def problems(self, topology=None):
        """Reasons this cannot start. Empty when it can."""
        issues = []
        if not (self.cpu_enabled or self.ram_enabled or self.gpu_enabled):
            issues.append("Nothing is selected to stress.")
        if self.cpu_enabled:
            top = topology or cpu_topology()
            if not top.selection(self.cpu_workers, self.cpu_physical_only):
                issues.append("No CPUs are available to load.")
            if not 1 <= self.cpu_percent <= 100:
                issues.append("CPU load must be between 1 and 100 percent.")
        if self.ram_enabled and self.ram_bytes <= 0:
            issues.append("Choose how much memory to allocate.")
        if self.gpu_enabled and not (self.gpu_percent or self.vram_bytes):
            issues.append("Choose a GPU load or an amount of graphics memory.")
        if self.gpu_enabled and self.gpu_mode not in gpu_mode_keys():
            issues.append("Unknown GPU workload: %s." % self.gpu_mode)
        return issues


def memory_headroom():
    """(available, total) bytes, or (None, None) if psutil is unavailable."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return mem.available, mem.total
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Worker entry points - these run in child processes
# ---------------------------------------------------------------------------

def _mark_as_oom_victim():
    try:
        with open("/proc/self/oom_score_adj", "w") as handle:
            handle.write(str(OOM_ADJUST))
    except OSError:
        pass                            # not fatal, just less polite


def _cpu_worker(cpu, duty_value, stop):
    """Burn a fixed fraction of one CPU until told to stop.

    The duty is read from shared memory every slice rather than passed once, so
    the parent's correction loop can steer this without restarting it.
    """
    _mark_as_oom_victim()
    try:
        os.sched_setaffinity(0, {cpu})
    except (AttributeError, OSError):
        # Not every platform pins; the load still lands, just not where asked.
        pass

    accumulator = 0.0
    while not stop.is_set():
        duty = max(0.01, min(1.0, duty_value.value))
        start = time.perf_counter()
        deadline = start + SLICE_SECONDS * duty

        while time.perf_counter() < deadline:
            # Enough arithmetic per check that the clock is not most of the
            # work, few enough that the deadline is not badly overshot.
            for _ in range(512):
                accumulator = accumulator * 1.0000001 + 1.0

        remaining = SLICE_SECONDS - (time.perf_counter() - start)
        if remaining > 0:
            time.sleep(remaining)


def _ram_worker(total_bytes, stop, progress):
    """Commit memory and hold it, touching it so it stays resident.

    Allocating alone proves nothing: Linux would hand back address space it
    never backs with pages. Writing one byte per page forces the commit, which
    is what makes this a memory test rather than a bookkeeping exercise.
    """
    _mark_as_oom_victim()
    blocks = []
    committed = 0

    try:
        while committed < total_bytes and not stop.is_set():
            size = min(RAM_BLOCK, total_bytes - committed)
            block = bytearray(size)
            for offset in range(0, size, PAGE):
                block[offset] = 1
            blocks.append(block)
            committed += size
            progress.value = committed
    except MemoryError:
        log.warning("Stress: memory allocation stopped at %d bytes", committed)
        progress.value = committed

    # Keep touching it: idle pages are swap candidates, and a test whose memory
    # has been paged out is no longer testing memory.
    index = 0
    while not stop.is_set():
        if blocks:
            block = blocks[index % len(blocks)]
            for offset in range(0, len(block), PAGE * 16):
                block[offset] = (block[offset] + 1) & 0xFF
            index += 1
        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class StressStatus:
    """A snapshot of what the session is doing, for the UI to render."""

    running: bool = False
    elapsed: float = 0.0
    cpu_requested: int = 0
    cpu_measured: Optional[float] = None
    cpu_count: int = 0
    ram_requested: int = 0
    ram_committed: int = 0
    gpu_requested: int = 0
    gpu_measured: Optional[int] = None
    gpu_mode: str = ""
    vram_requested: int = 0
    vram_used: Optional[int] = None
    notes: List[str] = field(default_factory=list)


class StressSession:
    """Owns the running load, and is responsible for it stopping.

    Every process this starts is tracked and terminated in stop(), which the
    window calls from its close handler and from the main window's. Nothing
    here outlives the application on purpose.
    """

    def __init__(self, config, topology=None, gpu=None):
        self.config = config
        self.topology = topology or cpu_topology()
        self.gpu = gpu
        self._procs = []
        self._stop = None
        self._duty = None
        self._ram_progress = None
        self._gpu_state = None
        self._started = None
        self._cpus = []
        self._notes = []
        self._vram_baseline = None

    # ---- lifecycle ------------------------------------------------------

    @property
    def running(self):
        return any(p.is_alive() for p in self._procs)

    @property
    def loaded_cpus(self):
        """Which logical CPUs the workers were pinned to."""
        return list(self._cpus)

    def start(self):
        """Launch every enabled load. Raises nothing; notes what it could not do."""
        if self._procs:
            return
        ctx = mp.get_context("spawn") if _spawn_is_safer() else mp.get_context()
        self._stop = ctx.Event()
        self._started = time.monotonic()
        self._notes = []

        if self.config.cpu_enabled:
            self._start_cpu(ctx)
        if self.config.ram_enabled:
            self._start_ram(ctx)
        if self.config.gpu_enabled and self.gpu is not None:
            self._start_gpu(ctx)

    def _start_cpu(self, ctx):
        self._cpus = self.topology.selection(self.config.cpu_workers,
                                             self.config.cpu_physical_only)
        duty = self.config.cpu_percent / 100.0
        self._duty = ctx.Value("d", duty)
        for cpu in self._cpus:
            proc = ctx.Process(target=_cpu_worker,
                               args=(cpu, self._duty, self._stop), daemon=True)
            proc.start()
            self._procs.append(proc)
        log.info("Stress: %d CPU worker(s) on %s at %d%%",
                 len(self._cpus), self._cpus, self.config.cpu_percent)

    def _start_ram(self, ctx):
        self._ram_progress = ctx.Value("q", 0)
        proc = ctx.Process(target=_ram_worker,
                           args=(self.config.ram_bytes, self._stop,
                                 self._ram_progress), daemon=True)
        proc.start()
        self._procs.append(proc)
        log.info("Stress: committing %d bytes of memory", self.config.ram_bytes)

    def _start_gpu(self, ctx):
        from .stressgpu import gpu_worker

        self._vram_baseline = self.gpu.vram_used()
        self._gpu_state = ctx.Array("i", [0, 0, 0])   # started, vram_mb, failed
        proc = ctx.Process(
            target=gpu_worker,
            args=(self.config.gpu_percent, self.config.vram_bytes,
                  self._stop, self._gpu_state, self.config.gpu_mode),
            daemon=True)
        proc.start()
        self._procs.append(proc)
        log.info("Stress: GPU %s at %d%% with %d bytes of graphics memory",
                 self.config.gpu_mode, self.config.gpu_percent,
                 self.config.vram_bytes)

    def stop(self):
        """End every load and wait for the processes to actually be gone.

        Graphics memory is only returned when the process that allocated it
        exits, so "stopped" has to mean the process is gone, not that it was
        asked nicely.
        """
        if self._stop is not None:
            self._stop.set()

        for proc in self._procs:
            try:
                proc.join(timeout=3)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=2)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=1)
            except Exception:
                log.exception("Stress: could not stop a worker")

        self._procs = []
        self._stop = None
        self._started = None

    # ---- feedback -------------------------------------------------------

    def correct(self, measured_percent):
        """Nudge the CPU duty toward the requested load.

        Open loop lands a few points high at low settings, because the inner
        arithmetic overshoots each deadline slightly. Correcting against what
        actually happened removes that without needing to model it.
        """
        if self._duty is None or measured_percent is None:
            return
        target = self.config.cpu_percent
        error = (target - measured_percent) / 100.0
        adjusted = self._duty.value + error * CORRECTION_GAIN
        self._duty.value = max(0.01, min(1.0, adjusted))

    def status(self, measured_cpu=None):
        """Everything the window needs for one refresh."""
        notes = list(self._notes)
        gpu_measured = None
        vram_used = None
        vram_requested = 0

        if self.config.gpu_enabled and self.gpu is not None:
            gpu_measured = self.gpu.busy_percent()
            vram_used = self.gpu.vram_used()
            vram_requested = self.config.vram_bytes
            if self._gpu_state is not None:
                if self._gpu_state[2]:
                    notes.append("The GPU load could not start on this driver.")
                elif vram_requested and self._gpu_state[1]:
                    landed = self._gpu_state[1] * 1024 * 1024
                    if landed < vram_requested * 0.75:
                        notes.append(
                            "The driver placed only %d MB of the %d MB asked "
                            "for in graphics memory; the rest went to system "
                            "memory."
                            % (landed // (1024 * 1024),
                               vram_requested // (1024 * 1024)))

        return StressStatus(
            running=self.running,
            elapsed=0.0 if self._started is None else time.monotonic() - self._started,
            cpu_requested=self.config.cpu_percent if self.config.cpu_enabled else 0,
            cpu_measured=measured_cpu,
            cpu_count=len(self._cpus),
            ram_requested=self.config.ram_bytes if self.config.ram_enabled else 0,
            ram_committed=0 if self._ram_progress is None else self._ram_progress.value,
            gpu_requested=self.config.gpu_percent if self.config.gpu_enabled else 0,
            gpu_measured=gpu_measured,
            gpu_mode=self.config.gpu_mode if self.config.gpu_enabled else "",
            vram_requested=vram_requested,
            vram_used=vram_used,
            notes=notes,
        )


def _spawn_is_safer():
    """Whether to spawn children rather than fork them.

    Forking a process that already has Qt and an open GPU context duplicates
    state that was never meant to be duplicated. Spawn costs a fresh
    interpreter per worker, which is irrelevant next to the load itself.
    """
    return True
