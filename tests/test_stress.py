"""Stress test logic.

The parts worth pinning are the ones that differ between machines: how CPU
topology is read (the arrangement is genuinely vendor-specific), how a GPU is
measured when the driver does not publish utilisation, and that a session leaves
nothing running behind it.

Nothing here starts a real load except the one test that checks processes stop,
and that one asks for the smallest load it can.
"""

import multiprocessing as mp
import os
import shutil
import tempfile
import time
import unittest

from ksyssupervisor import series, stress


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class CpuTree(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def cpu(self, index, siblings=None, online=None):
        path = os.path.join(self.root, "cpu%d" % index)
        os.makedirs(path, exist_ok=True)
        if siblings is not None:
            write(os.path.join(path, "topology", "thread_siblings_list"),
                  siblings)
        if online is not None:
            write(os.path.join(path, "online"), online)
        return path


class TopologyTest(CpuTree):
    def test_amd_style_pairing(self):
        """This machine: 8 cores, siblings (n, n+8)."""
        for i in range(8):
            self.cpu(i, "%d,%d" % (i, i + 8))
            self.cpu(i + 8, "%d,%d" % (i, i + 8))
        top = stress.cpu_topology(self.root)
        self.assertEqual(top.logical, list(range(16)))
        self.assertEqual(top.physical, list(range(8)))
        self.assertTrue(top.has_smt)

    def test_intel_style_pairing(self):
        """Adjacent siblings (2n, 2n+1) - the same code, no vendor check."""
        for core in range(4):
            a, b = core * 2, core * 2 + 1
            self.cpu(a, "%d,%d" % (a, b))
            self.cpu(b, "%d,%d" % (a, b))
        top = stress.cpu_topology(self.root)
        self.assertEqual(top.physical, [0, 2, 4, 6])
        self.assertTrue(top.has_smt)

    def test_efficiency_cores_without_siblings(self):
        """Intel P/E: the P-cores are paired, the E-cores are not."""
        for core in range(2):                       # P-cores
            a, b = core * 2, core * 2 + 1
            self.cpu(a, "%d,%d" % (a, b))
            self.cpu(b, "%d,%d" % (a, b))
        for index in (4, 5, 6, 7):                  # E-cores
            self.cpu(index, str(index))
        top = stress.cpu_topology(self.root)
        self.assertEqual(top.logical, [0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(top.physical, [0, 2, 4, 5, 6, 7])

    def test_no_smt_at_all(self):
        """Most ARM parts, and desktop chips with it switched off."""
        for index in range(4):
            self.cpu(index, str(index))
        top = stress.cpu_topology(self.root)
        self.assertEqual(top.physical, top.logical)
        self.assertFalse(top.has_smt)

    def test_offline_cpus_are_not_offered(self):
        """Scheduling onto an offline CPU fails, so it must not be listed."""
        self.cpu(0, "0,1")
        self.cpu(1, "0,1")
        self.cpu(2, "2,3", online="0")
        self.cpu(3, "2,3", online="0")
        top = stress.cpu_topology(self.root)
        self.assertEqual(top.logical, [0, 1])

    def test_non_cpu_directories_are_ignored(self):
        """cpuidle and cpufreq live alongside the cpuN entries."""
        self.cpu(0, "0")
        os.makedirs(os.path.join(self.root, "cpuidle"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "cpufreq"), exist_ok=True)
        self.assertEqual(stress.cpu_topology(self.root).logical, [0])

    def test_a_tree_with_no_topology_still_yields_cpus(self):
        """Some containers expose no topology; refusing to run would be worse
        than running without the physical distinction."""
        top = stress.cpu_topology(os.path.join(self.root, "nothing-here"))
        self.assertTrue(top.logical)
        self.assertEqual(top.physical, top.logical)


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.top = stress.CpuTopology(logical=list(range(16)),
                                      physical=list(range(8)))

    def test_physical_only_never_picks_a_sibling(self):
        chosen = self.top.selection(8, physical_only=True)
        self.assertEqual(chosen, list(range(8)))

    def test_threads_allowed_still_spreads_over_cores_first(self):
        """Four workers on an 8-core/16-thread part belong on four cores, not
        doubled up on two."""
        self.assertEqual(self.top.selection(4, physical_only=False),
                         [0, 1, 2, 3])

    def test_more_than_available_is_capped(self):
        self.assertEqual(len(self.top.selection(99, physical_only=True)), 8)
        self.assertEqual(len(self.top.selection(99, physical_only=False)), 16)

    def test_at_least_one(self):
        self.assertEqual(len(self.top.selection(0, physical_only=True)), 1)

    def test_empty_topology_selects_nothing(self):
        empty = stress.CpuTopology()
        self.assertEqual(empty.selection(4, physical_only=True), [])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.top = stress.CpuTopology(logical=[0, 1], physical=[0])

    def test_nothing_selected_is_refused(self):
        problems = stress.StressConfig().problems(self.top)
        self.assertTrue(any("Nothing is selected" in p for p in problems))

    def test_a_valid_cpu_run_has_no_problems(self):
        config = stress.StressConfig(cpu_enabled=True, cpu_workers=1,
                                     cpu_percent=50)
        self.assertEqual(config.problems(self.top), [])

    def test_load_outside_the_range_is_refused(self):
        config = stress.StressConfig(cpu_enabled=True, cpu_percent=0)
        self.assertTrue(any("between 1 and 100" in p
                            for p in config.problems(self.top)))

    def test_memory_run_needs_an_amount(self):
        config = stress.StressConfig(ram_enabled=True, ram_bytes=0)
        self.assertTrue(any("how much memory" in p
                            for p in config.problems(self.top)))

    def test_gpu_run_needs_load_or_memory(self):
        config = stress.StressConfig(gpu_enabled=True, gpu_percent=0,
                                     vram_bytes=0)
        self.assertTrue(any("GPU load" in p for p in config.problems(self.top)))

    def test_an_unknown_gpu_workload_is_refused(self):
        config = stress.StressConfig(gpu_enabled=True, gpu_percent=50,
                                     gpu_mode="no-such-shader")
        self.assertTrue(any("Unknown GPU workload" in p
                            for p in config.problems(self.top)))

    def test_the_default_gpu_workload_is_accepted(self):
        config = stress.StressConfig(gpu_enabled=True, gpu_percent=50)
        self.assertEqual(config.problems(self.top), [])


class GpuModeTableTest(unittest.TestCase):
    """The mode table and the shaders live in different modules - stress.py is
    the Qt-free half - so nothing but a test keeps the two halves in step."""

    def setUp(self):
        try:
            from ksyssupervisor import stressgpu
        except ImportError:                             # pragma: no cover
            self.skipTest("PyQt6 is not available")
        self.gpu = stressgpu

    def test_every_mode_resolves_to_a_shader(self):
        for key in stress.gpu_mode_keys():
            names = self.gpu.workload_names(key)
            self.assertTrue(names, "%s runs nothing" % key)
            for name in names:
                self.assertIn(name, self.gpu.FRAGMENT_SOURCES)

    def test_every_shader_is_reachable_from_a_mode(self):
        reachable = set()
        for key in stress.gpu_mode_keys():
            reachable.update(self.gpu.workload_names(key))
        self.assertEqual(reachable, set(self.gpu.FRAGMENT_SOURCES),
                         "a shader no mode can select, or the other way round")

    def test_every_mode_is_explained(self):
        for _key, label, text in stress.GPU_MODES:
            self.assertTrue(label.strip())
            self.assertGreater(len(text.strip()), 40, label)

    def test_an_unknown_mode_still_runs_something(self):
        """A setting saved by an older build must not fail the test."""
        names = self.gpu.workload_names("gone-away")
        self.assertEqual(names, [self.gpu.FALLBACK_MODE])

    def test_the_default_mode_is_in_the_table(self):
        self.assertIn(stress.DEFAULT_GPU_MODE, stress.gpu_mode_keys())


class GpuTargetTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_amd_reads_utilisation_from_sysfs(self):
        write(os.path.join(self.root, "gpu_busy_percent"), "42")
        write(os.path.join(self.root, "mem_info_vram_used"), "1234")
        target = stress.GpuTarget(key="gpu:0", name="Card", driver="amdgpu",
                                  path=self.root)
        self.assertEqual(target.busy_percent(), 42)
        self.assertEqual(target.vram_used(), 1234)
        self.assertTrue(target.measurable)

    def test_a_driver_that_publishes_nothing_is_reported_unmeasurable(self):
        """i915 and xe have no gpu_busy_percent. The load still runs; the UI
        just must not show a utilisation it never read."""
        target = stress.GpuTarget(key="gpu:0", name="Card", driver="i915",
                                  path=self.root)
        self.assertIsNone(target.busy_percent())
        self.assertFalse(target.measurable)

    def test_nvidia_goes_through_nvidia_smi(self):
        calls = []

        def fake_run(argv, *args, **kwargs):
            calls.append(argv)
            if "utilization.gpu" in " ".join(argv):
                return "77\n"
            return "2048\n"

        original = stress.run_cmd
        stress.run_cmd = fake_run
        self.addCleanup(setattr, stress, "run_cmd", original)

        target = stress.GpuTarget(key="gpu:0", name="Card", driver="nvidia",
                                  path=self.root, nvidia_index=0)
        self.assertEqual(target.busy_percent(), 77)
        self.assertEqual(target.vram_used(), 2048 * 1024 * 1024)
        self.assertTrue(calls)

    def test_nvidia_absent_is_not_an_error(self):
        original = stress.run_cmd
        stress.run_cmd = lambda *a, **k: None
        self.addCleanup(setattr, stress, "run_cmd", original)
        target = stress.GpuTarget(key="gpu:0", name="Card", driver="nvidia",
                                  path=self.root, nvidia_index=0)
        self.assertIsNone(target.busy_percent())


class NvidiaIndexTest(unittest.TestCase):
    def test_bus_ids_are_normalised_to_sysfs_form(self):
        """nvidia-smi prints 00000000:03:00.0; sysfs uses 0000:03:00.0."""
        original = stress.run_cmd
        stress.run_cmd = lambda *a, **k: "0, 00000000:03:00.0\n1, 00000000:0A:00.0\n"
        self.addCleanup(setattr, stress, "run_cmd", original)
        found = stress._nvidia_indexes()
        self.assertEqual(found.get("0000:03:00.0"), 0)
        self.assertEqual(found.get("0000:0a:00.0"), 1)

    def test_no_nvidia_smi_gives_an_empty_map(self):
        original = stress.run_cmd
        stress.run_cmd = lambda *a, **k: None
        self.addCleanup(setattr, stress, "run_cmd", original)
        self.assertEqual(stress._nvidia_indexes(), {})


class CorrectionTest(unittest.TestCase):
    """The closed loop that pulls measured load towards what was asked for."""

    def session(self, requested):
        config = stress.StressConfig(cpu_enabled=True, cpu_percent=requested)
        session = stress.StressSession(config)
        session._duty = mp.Value("d", requested / 100.0)
        return session

    def test_measuring_low_raises_the_duty(self):
        session = self.session(50)
        before = session._duty.value
        session.correct(40.0)
        self.assertGreater(session._duty.value, before)

    def test_measuring_high_lowers_the_duty(self):
        session = self.session(50)
        before = session._duty.value
        session.correct(60.0)
        self.assertLess(session._duty.value, before)

    def test_the_duty_stays_within_bounds(self):
        session = self.session(100)
        for _ in range(50):
            session.correct(0.0)
        self.assertLessEqual(session._duty.value, 1.0)

        session = self.session(1)
        for _ in range(50):
            session.correct(100.0)
        self.assertGreater(session._duty.value, 0.0)

    def test_no_measurement_changes_nothing(self):
        session = self.session(50)
        before = session._duty.value
        session.correct(None)
        self.assertEqual(session._duty.value, before)


class SessionLifecycleTest(unittest.TestCase):
    """Whatever a session starts, it has to stop - this is the safety property."""

    def test_stop_leaves_no_worker_running(self):
        config = stress.StressConfig(cpu_enabled=True, cpu_workers=1,
                                     cpu_physical_only=True, cpu_percent=1)
        session = stress.StressSession(config)
        session.start()
        self.addCleanup(session.stop)

        deadline = time.time() + 5
        while not session.running and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(session.running, "the worker never started")

        # Kept before stop() clears the list, so the exit codes can be checked
        # afterwards: is_alive() goes true the moment start() returns, well
        # before the child has run anything, so on its own it would not
        # distinguish a working worker from one that died on import.
        procs = list(session._procs)
        self.assertTrue(all(p.pid for p in procs))

        session.stop()
        self.assertFalse(session.running)
        self.assertEqual(session._procs, [])
        for proc in procs:
            self.assertIsNotNone(proc.exitcode,
                                 "a worker was left without an exit code")
            self.assertNotEqual(proc.exitcode, 1,
                                "the worker died of an exception")

    def test_stopping_twice_is_harmless(self):
        session = stress.StressSession(stress.StressConfig())
        session.stop()
        session.stop()
        self.assertFalse(session.running)

    def test_status_of_an_idle_session_is_not_running(self):
        session = stress.StressSession(stress.StressConfig())
        state = session.status()
        self.assertFalse(state.running)
        self.assertEqual(state.elapsed, 0.0)


class MemoryHeadroomTest(unittest.TestCase):
    def test_reports_plausible_numbers(self):
        available, total = stress.memory_headroom()
        if available is None:
            self.skipTest("psutil is not available")
        self.assertGreater(total, 0)
        self.assertLessEqual(available, total)


if __name__ == "__main__":
    unittest.main()


class SafetyWindowTest(unittest.TestCase):
    """The two things that make an unattended test safe to leave running."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt6.QtWidgets import QApplication
        except ImportError:                             # pragma: no cover
            raise unittest.SkipTest("PyQt6 is not available")
        cls.app = QApplication.instance() or QApplication([])

    def window(self, limit=95, abort=True):
        from PyQt6.QtCore import QSettings
        from ksyssupervisor import stresswindow

        window = stresswindow.StressWindow(
            settings=QSettings("KSysSupervisorTest", "Safety"))
        self.addCleanup(self._shut, window)
        window.cpu_box.setChecked(True)
        window.ram_box.setChecked(False)
        window.gpu_box.setChecked(False)
        window.cpu_workers.setCurrentIndex(0)      # one worker
        window.cpu_percent.setValue(1)
        window.abort_choice.setChecked(abort)
        window.warn_choice.setChecked(not abort)
        window.limit_spin.setValue(limit)
        return window

    @staticmethod
    def _shut(window):
        window._closed = True
        window.shutdown()

    @staticmethod
    def _sample(celsius):
        from ksyssupervisor.providers.base import Reading

        class Sample:
            readings = [Reading(device="cpu", group="Temperatures",
                                key="pkg", label="Package",
                                value=celsius, kind="temp")]
        return Sample()

    def test_the_limit_stops_the_test(self):
        window = self.window(limit=70)
        window.start()
        self.assertTrue(window.is_running())

        window.take_sample(self._sample(55.0))
        self.assertTrue(window.is_running(), "stopped below the limit")

        window.take_sample(self._sample(72.0))
        self.assertFalse(window.is_running(), "did not stop at the limit")
        self.assertIn("70", window.status.text())

    def test_a_shared_label_cannot_hide_the_hotter_reading(self):
        """Two devices can report the same label. Keyed by label, whichever
        came last won, and a cool GPU "Package" masked a hot CPU one."""
        from ksyssupervisor.providers.base import Reading

        class Sample:
            readings = [
                Reading(device="cpu", group="Temperatures", key="Tctl",
                        label="Package", value=90.0, kind="temp"),
                Reading(device="gpu:0000:03:00.0", group="Temperatures",
                        key="temp2", label="Package", value=50.0, kind="temp")]

        window = self.window(limit=70)
        window.start()
        window.take_sample(Sample())
        self.assertFalse(window.is_running(), "the hot reading was hidden")
        self.assertIn("90", window.status.text())

    def test_warn_only_keeps_going(self):
        """The user chose this knowingly; nothing may override it."""
        window = self.window(abort=False)
        window.session = stress.StressSession(window._config(),
                                              window.topology, None)
        window.session.start()
        window._set_running(True)

        window.take_sample(self._sample(99.0))
        self.assertTrue(window.is_running())

    def test_a_blind_limit_stops_the_test(self):
        """A limit that stops being fed protects nothing, and a test left
        running under one would be unprotected while appearing watched."""
        from ksyssupervisor import stresswindow

        original = stresswindow.SAMPLE_TIMEOUT_S
        stresswindow.SAMPLE_TIMEOUT_S = 0.5
        self.addCleanup(setattr, stresswindow, "SAMPLE_TIMEOUT_S", original)

        window = self.window()
        window.start()
        window.take_sample(self._sample(40.0))
        self.assertTrue(window.is_running())

        time.sleep(0.8)                         # readings stop arriving
        window._refresh()
        self.assertFalse(window.is_running())
        self.assertIn("no temperature readings", window.status.text())

    def test_threads_grey_out_rather_than_vanish(self):
        """Ticking "physical cores only" has to take the extra CPUs away in a
        way the user can see, and take the selection down with them."""
        window = self.window()
        top = window.topology
        if not top.has_smt:
            self.skipTest("this machine has no SMT to hide")

        combo = window.cpu_workers
        self.assertEqual(combo.count(), len(top.logical))

        def enabled(row):
            return combo.model().item(row).isEnabled()

        window.cpu_physical.setChecked(False)
        self.assertTrue(all(enabled(r) for r in range(combo.count())))

        # A count only the threads can satisfy, then the pool shrinks under it.
        combo.setCurrentIndex(len(top.logical) - 1)
        window.cpu_physical.setChecked(True)

        self.assertTrue(enabled(len(top.physical) - 1))
        self.assertFalse(enabled(len(top.physical)),
                         "a thread-only count stayed selectable")
        self.assertEqual(combo.currentData(), len(top.physical),
                         "the selection stayed above the limit")
        self.assertEqual(window._config().cpu_workers, len(top.physical))

    def test_the_gpu_workload_reaches_the_config(self):
        window = self.window()
        for row in range(window.gpu_mode.count()):
            window.gpu_mode.setCurrentIndex(row)
            key = window.gpu_mode.currentData()
            self.assertIn(key, stress.gpu_mode_keys())
            self.assertEqual(window._config().gpu_mode, key)

    def test_warn_only_is_not_stopped_by_the_watchdog(self):
        from ksyssupervisor import stresswindow

        original = stresswindow.SAMPLE_TIMEOUT_S
        stresswindow.SAMPLE_TIMEOUT_S = 0.5
        self.addCleanup(setattr, stresswindow, "SAMPLE_TIMEOUT_S", original)

        window = self.window(abort=False)
        window.session = stress.StressSession(window._config(),
                                              window.topology, None)
        window.session.start()
        window._set_running(True)
        window._last_sample = time.monotonic() - 5
        window._refresh()
        self.assertTrue(window.is_running())


class StressGraphTest(unittest.TestCase):
    """Starting a test opens the graphs of the hardware it loads."""

    # The safety tests' window, borrowed rather than inherited so their tests
    # do not run a second time under this class.
    setUpClass = SafetyWindowTest.__dict__["setUpClass"]
    window = SafetyWindowTest.window
    _shut = SafetyWindowTest.__dict__["_shut"]

    @staticmethod
    def models():
        from ksyssupervisor.display import TileBuilder
        from ksyssupervisor.providers.base import Device, Reading

        builder = TileBuilder({"cpu": Device("cpu", "Ryzen", 10),
                               "ram": Device("ram", "RAM", 20),
                               "gpu:0000:03:00.0": Device("gpu:0000:03:00.0",
                                                          "RX 6800", 30)})
        builder.build([
            Reading("cpu", "Temperatures", "Tctl", "Package", 50.0, "temp"),
            Reading("cpu", "Utilization", "usage_total", "Processor", 3.0,
                    "utilization"),
            Reading("cpu", "Clocks", "clock_0", "Core #0", 4000.0, "clock"),
            Reading("ram", "Utilization", "usage", "Memory Used", 6.0,
                    "memory_gb", total=32.0),
            Reading("gpu:0000:03:00.0", "Temperatures", "junction",
                    "Hot Spot", 55.0, "temp"),
        ])
        return builder.tree()

    def test_the_cpu_test_graphs_the_cpu(self):
        window = self.window()
        window.show()
        window.feed_graphs(self.models())
        before = window.width()
        window.start()
        self.assertTrue(window.graphs.isVisible())
        self.assertGreater(window.width(), before)
        # The package and the load; not one strip per core clock.
        self.assertEqual(window.graphs.selection(),
                         ["cpu/Tctl", "cpu/usage_total"])

    def test_the_choice_waits_for_the_first_reading(self):
        window = self.window()
        window.show()
        window.start()
        self.assertEqual(window.graphs.selection(), [])
        window.feed_graphs(self.models())
        self.assertEqual(window.graphs.selection(),
                         ["cpu/Tctl", "cpu/usage_total"])

    def test_each_loaded_device_is_graphed_in_order(self):
        from ksyssupervisor.stresswindow import StressWindow

        config = stress.StressConfig(cpu_enabled=True, ram_enabled=True,
                                     gpu_enabled=True,
                                     gpu_key="gpu:0000:03:00.0")
        self.assertEqual(StressWindow.stressed_devices(config),
                         ["cpu", "ram", "gpu:0000:03:00.0"])
        self.assertEqual(
            series.stress_graphs(self.models(),
                                 StressWindow.stressed_devices(config)),
            ["cpu/Tctl", "cpu/usage_total", "ram/usage",
             "gpu:0000:03:00.0/junction"])

    def test_hiding_the_graphs_gives_the_width_back(self):
        window = self.window()
        window.show()
        before = window.width()
        window.graphs_button.setChecked(True)
        window.graphs_button.setChecked(False)
        self.assertEqual(window.width(), before)
        self.assertEqual(window.graphs_button.text(), "Show Graphs")
