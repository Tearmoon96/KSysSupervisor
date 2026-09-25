"""The stress test window.

A top-level window rather than a dialog, for the same reason the fan control one
is: it is meant to sit next to the sensor tree so you can watch temperatures and
clocks move while the load is on.

The rules about what may be run live in stress.py; this is presentation, plus
the safety choice the user has to make before anything starts.
"""

import time

from PyQt6.QtCore import QSettings, Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFrame,
                             QGridLayout, QHBoxLayout, QLabel,
                             QMessageBox, QPushButton, QRadioButton,
                             QSizePolicy, QSlider, QSpacerItem, QSpinBox,
                             QVBoxLayout, QWidget)

from . import APP_NAME, log, series, stress
from .geometry import fit_on_screen, screen_limits
from .livegraph import PANEL_WIDTH, LiveGraphPanel, width_after_panel
from .widgets import THEME_EVENTS, SectionCard, paint_backdrop, tool_tip


def _tip(title, text):
    return tool_tip("stress", title, text)

GB = 1024 ** 3

REFRESH_MS = 1000

#: How long the window will run with a temperature limit armed but no readings
#: arriving before it stops the test itself. The limit is only a safeguard for
#: as long as it is being fed: if the sensor worker dies, a test left running
#: would be unprotected while still appearing to be watched.
SAMPLE_TIMEOUT_S = 10.0

#: Default cut-off when automatic stopping is chosen. Below the point most
#: desktop parts start throttling, so a healthy machine never reaches it by
#: accident, and well below anything that damages hardware.
DEFAULT_LIMIT_C = 90

#: Leave this much memory to the rest of the system in the suggested default.
#: The slider can still be pushed past it - deliberately - with a warning.
RAM_HEADROOM = 2 * GB

#: Percentage and size sliders are coarse choices; stretched across the whole
#: window they buy no precision and leave each value marooned far from the label
#: it belongs to. The graphics card list is capped for the same reason - the
#: name is short, and a full-width combo box looks like it is waiting for more.
SLIDER_WIDTH = 260
COMBO_WIDTH = 340

#: How wide the workload explanation opens. A QMessageBox sizes itself to its
#: text and does not wrap rich text on its own, so without this the four
#: paragraphs come out as four very long lines.
HELP_WIDTH = 520


def _sized(widget, width):
    """Give a control a set width instead of the whole row.

    Both bounds, not just the maximum: with the stretch taken off its column a
    slider collapses to its minimum size hint, which is tiny, and the three
    sections ended up with visibly different slider lengths.
    """
    widget.setMinimumWidth(width)
    widget.setMaximumWidth(width)
    return widget


class StressWindow(QWidget):
    """Configure and run a stress test, and watch what it does."""

    def __init__(self, settings=None, store=None):
        # Parentless on purpose: a real top-level window with its own task
        # manager entry, closable independently of the main window.
        super().__init__(None)
        self.setWindowTitle("%s - Stress Test" % APP_NAME)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self.settings = settings or QSettings("KSysSupervisor", "Settings")
        self.topology = stress.cpu_topology()
        self.targets = stress.gpu_targets()
        self.session = None
        self._closed = False
        # uid -> (label, celsius), from the main worker. Keyed by reading, not
        # by label: two cards both have a "GPU", two drives a "Composite", and
        # keyed by label the cooler one could hide the hotter from the limit.
        self._temperatures = {}
        self._peak = {}
        self._stopped_by_limit = False
        self._was_enabled = {}
        self._last_sample = None

        # The graphs beside the controls. History is the main window's store,
        # recorded from startup, so the strips open onto the minutes before
        # the test began: the idle baseline is half of what they show.
        self.store = store if store is not None else series.SeriesStore()
        self._models = []
        self._graph_devices = []
        self._pick_pending = False
        # (width before, width after) the graphs widened this window, so
        # hiding them can put it back; None when they did not widen it.
        self._graphs_widened = None

        root = QHBoxLayout(self)
        controls = QWidget()
        layout = QVBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(controls, 0)

        self.graphs = LiveGraphPanel()
        self.graphs.hide()
        root.addWidget(self.graphs, 1)

        layout.addWidget(self._intro())
        layout.addWidget(self._cpu_group())
        layout.addWidget(self._ram_group())
        layout.addWidget(self._gpu_group())
        layout.addWidget(self._safety_group())

        self.status = QLabel("Not running.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start)
        self.start_button.setToolTip(_tip(
            "Start", "Run every ticked test at once. The graphs of the "
            "hardware under load open beside these settings."))
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setToolTip(_tip(
            "Stop", "End the test now and free the memory it took."))
        buttons.addWidget(self.stop_button)
        self.graphs_button = QPushButton("Show Graphs")
        self.graphs_button.setCheckable(True)
        self.graphs_button.setToolTip(_tip(
            "Graphs", "Live graphs of the hardware under load, beside these "
            "settings. They open by themselves when a test starts."))
        self.graphs_button.toggled.connect(self.set_graphs_visible)
        buttons.addWidget(self.graphs_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        close.setToolTip(_tip("Close", "Close this window. A running test "
                              "is stopped first, after asking."))
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_MS)

        self._restore_geometry()
        self._update_ram_warning()

    # ---- backdrop -------------------------------------------------------

    def paintEvent(self, event):
        paint_backdrop(self)
        super().paintEvent(event)

    def changeEvent(self, event):
        if event.type() in THEME_EVENTS:
            self.update()
        super().changeEvent(event)

    # ---- construction ---------------------------------------------------

    def _intro(self):
        text = QLabel(
            "Loads the hardware on purpose, to see how it behaves when hot. "
            "Everything runs in separate processes, so stopping the test - or "
            "closing %s - ends it." % APP_NAME)
        text.setWordWrap(True)
        return text

    def _cpu_group(self):
        box = SectionCard("Processor", checkable=True, checked=True)
        # Named for the test that drives it: tests/test_stress.py reaches in
        # by these attribute names to set up an unattended run.
        self.cpu_box = box
        box.setToolTip(_tip("Processor", "Tick to load the CPU: one busy "
                            "worker per chosen core."))
        grid = QGridLayout(box.body)

        physical = len(self.topology.physical)
        logical = len(self.topology.logical)

        grid.addWidget(QLabel("Workers:"), 0, 0)
        # A list of every logical CPU rather than a spin box: the range is
        # small and known, so the whole choice is worth showing at once. The
        # entries past the physical core count stay in the list and are greyed
        # instead of disappearing, so ticking "Physical cores only" visibly
        # takes options away rather than silently shortening the list.
        self.cpu_workers = _sized(QComboBox(), 90)
        for count in range(1, logical + 1):
            self.cpu_workers.addItem(str(count), count)
        self._select_workers(physical)
        self.cpu_workers.setToolTip(_tip(
            "Workers", "How many cores to load, each pinned to its own "
            "core."))
        grid.addWidget(self.cpu_workers, 0, 1)

        self.cpu_physical = QCheckBox("Physical cores only")
        self.cpu_physical.setChecked(True)
        if not self.topology.has_smt:
            # Nothing to distinguish: every logical CPU is a core of its own.
            self.cpu_physical.setEnabled(False)
            self.cpu_physical.setToolTip(_tip(
                "Physical cores only",
                "This processor has no simultaneous multithreading, so every "
                "logical CPU is already a separate core."))
        else:
            self.cpu_physical.setToolTip(_tip(
                "Physical cores only",
                "One worker per real core, leaving each core's second "
                "thread idle. Untick to load every thread."))
        self.cpu_physical.toggled.connect(self._update_cpu_limit)
        grid.addWidget(self.cpu_physical, 0, 2)

        self.cpu_summary = QLabel("")
        self.cpu_summary.setWordWrap(True)
        grid.addWidget(self.cpu_summary, 1, 0, 1, 3)

        grid.addWidget(QLabel("Load:"), 2, 0)
        self.cpu_percent = _sized(QSlider(Qt.Orientation.Horizontal), SLIDER_WIDTH)
        self.cpu_percent.setRange(1, 100)
        self.cpu_percent.setValue(100)
        self.cpu_percent.setToolTip(_tip(
            "Load", "How much of each second the workers are busy. Below "
            "100 % they rest the rest of it."))
        grid.addWidget(self.cpu_percent, 2, 1)
        self.cpu_percent_label = QLabel("100 %")
        self.cpu_percent.valueChanged.connect(
            lambda v: self.cpu_percent_label.setText("%d %%" % v))
        grid.addWidget(self.cpu_percent_label, 2, 2)

        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(3, 1)
        self._update_cpu_limit()
        return box

    def _ram_group(self):
        box = SectionCard("Memory", checkable=True)
        self.ram_box = box
        box.setToolTip(_tip("Memory", "Tick to fill and keep rewriting "
                            "memory, which checks it as well as loading it."))
        grid = QGridLayout(box.body)

        available, total = stress.memory_headroom()
        self._available = available or 0
        self._total = total or 0
        cap = max(1, int((self._total or GB) / GB))

        grid.addWidget(QLabel("Allocate:"), 0, 0)
        self.ram_gb = _sized(QSlider(Qt.Orientation.Horizontal), SLIDER_WIDTH)
        self.ram_gb.setRange(1, cap)
        suggested = max(1, int((self._available - RAM_HEADROOM) / GB))
        self.ram_gb.setValue(min(suggested, cap))
        self.ram_gb.valueChanged.connect(self._update_ram_warning)
        self.ram_gb.setToolTip(_tip(
            "Allocate", "How much memory the test takes. Past what is free, "
            "the system starts swapping and everything slows down."))
        grid.addWidget(self.ram_gb, 0, 1)
        self.ram_label = QLabel("")
        grid.addWidget(self.ram_label, 0, 2)

        self.ram_note = QLabel("")
        self.ram_note.setWordWrap(True)
        grid.addWidget(self.ram_note, 1, 0, 1, 3)

        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(3, 1)
        return box

    def _gpu_group(self):
        box = SectionCard("Graphics", checkable=True)
        self.gpu_box = box
        grid = QGridLayout(box.body)

        self.gpu_choice = _sized(QComboBox(), COMBO_WIDTH)
        for target in self.targets:
            self.gpu_choice.addItem(target.name, target.key)
        self.gpu_choice.setToolTip(_tip("Graphics card",
                                        "Which card to load."))
        grid.addWidget(self.gpu_choice, 0, 0, 1, 3)

        grid.addWidget(QLabel("Workload:"), 1, 0)
        # Sized to the sliders below rather than to COMBO_WIDTH, so the column
        # lines up and the "?" sits immediately beside the choice it explains.
        self.gpu_mode = _sized(QComboBox(), SLIDER_WIDTH)
        for key, label, _text in stress.GPU_MODES:
            # Marked here rather than in the table so the status line can name
            # a workload without reading "transcendental (default)".
            if key == stress.DEFAULT_GPU_MODE:
                label += " (default)"
            self.gpu_mode.addItem(label, key)
        self.gpu_mode.setToolTip(_tip(
            "Workload", "What the card is made to do. Press ? for what "
            "each one loads."))
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(self.gpu_mode)
        self.gpu_mode_help = QPushButton("?")
        self.gpu_mode_help.setFixedWidth(28)
        self.gpu_mode_help.setToolTip(_tip("Workloads",
                                           "What each workload does."))
        self.gpu_mode_help.clicked.connect(self._explain_gpu_modes)
        mode_row.addWidget(self.gpu_mode_help)
        mode_row.addStretch(1)
        grid.addLayout(mode_row, 1, 1, 1, 2)

        grid.addWidget(QLabel("Load:"), 2, 0)
        self.gpu_percent = _sized(QSlider(Qt.Orientation.Horizontal), SLIDER_WIDTH)
        self.gpu_percent.setRange(0, 100)
        self.gpu_percent.setValue(100)
        self.gpu_percent.setToolTip(_tip(
            "Load", "How busy to keep the card. Measured back where the "
            "driver reports utilisation."))
        grid.addWidget(self.gpu_percent, 2, 1)
        self.gpu_percent_label = QLabel("100 %")
        self.gpu_percent.valueChanged.connect(
            lambda v: self.gpu_percent_label.setText("%d %%" % v))
        grid.addWidget(self.gpu_percent_label, 2, 2)

        grid.addWidget(QLabel("Graphics memory:"), 3, 0)
        self.vram_gb = _sized(QSlider(Qt.Orientation.Horizontal), SLIDER_WIDTH)
        vram_cap = 0
        for target in self.targets:
            if target.vram_total:
                vram_cap = max(vram_cap, int(target.vram_total / GB))
        self.vram_gb.setRange(0, max(1, vram_cap))
        self.vram_gb.setValue(0)
        self.vram_gb.setToolTip(_tip(
            "Graphics memory", "Also fill this much of the card's memory. "
            "Off loads the card without touching its memory."))
        grid.addWidget(self.vram_gb, 3, 1)
        self.vram_label = QLabel("off")
        self.vram_gb.valueChanged.connect(
            lambda v: self.vram_label.setText("off" if not v else "%d GB" % v))
        grid.addWidget(self.vram_label, 3, 2)

        self.gpu_note = QLabel("")
        self.gpu_note.setWordWrap(True)
        note_font = QFont(self.gpu_note.font())
        note_font.setItalic(True)
        self.gpu_note.setFont(note_font)
        grid.addWidget(self.gpu_note, 4, 0, 1, 3)

        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(3, 1)

        if not self.targets:
            box.setChecked(False)
            box.setEnabled(False)
            box.setToolTip(_tip("Graphics", "No graphics card was found "
                                "that can be loaded."))
        else:
            box.setToolTip(_tip("Graphics", "Tick to load the graphics "
                                "card."))
            target = self._selected_gpu()
            if target is not None and not target.measurable:
                # Said plainly rather than shown as a number we did not check.
                self.gpu_note.setText(
                    "This driver does not report GPU utilisation, so the load "
                    "is applied as requested but cannot be measured back.")
        return box

    def _safety_group(self):
        box = SectionCard("If it gets hot")
        layout = QVBoxLayout(box.body)

        self.abort_choice = QRadioButton("Stop the test automatically above")
        self.abort_choice.setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.abort_choice)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(40, 110)
        self.limit_spin.setValue(DEFAULT_LIMIT_C)
        self.limit_spin.setSuffix(" °C")
        self.abort_choice.setToolTip(_tip(
            "Stop automatically", "End the test the moment any temperature "
            "on the machine reaches this limit."))
        self.limit_spin.setToolTip(_tip(
            "Limit", "The temperature that stops the test."))
        row.addWidget(self.limit_spin)
        row.addStretch(1)
        layout.addLayout(row)

        self.warn_choice = QRadioButton("Only warn me, and keep going")
        self.warn_choice.setToolTip(_tip(
            "Only warn", "Keep running however hot it gets. Only for when "
            "you are watching it."))
        layout.addWidget(self.warn_choice)

        self.disclaimer = QLabel(
            "<b>Continuing to run a stress test at high temperatures can "
            "permanently damage your hardware.</b> If you choose this, you do "
            "so at your own risk: the author of this software accepts no "
            "responsibility for any damage that results.")
        self.disclaimer.setWordWrap(True)
        self.disclaimer.setVisible(False)
        self.warn_choice.toggled.connect(self.disclaimer.setVisible)
        layout.addWidget(self.disclaimer)

        self.abort_choice.toggled.connect(self.limit_spin.setEnabled)
        return box

    # ---- helpers --------------------------------------------------------

    def _explain_gpu_modes(self):
        """What each workload actually stresses.

        The text comes from stress.GPU_MODES rather than being written out
        again here, so a workload cannot end up in the list with no
        explanation or be explained after it has been removed.
        """
        parts = ["<p>All four put the card under load; they differ in which "
                 "part of it runs out first.</p>"]
        for _key, label, text in stress.GPU_MODES:
            parts.append("<p><b>%s</b><br>%s</p>" % (label, text))

        self._gpu_mode_help_box("".join(parts)).exec()

    def _gpu_mode_help_box(self, body):
        """Built apart from showing it so the layout can be screenshotted.

        The width is set here because a QMessageBox sizes itself to its rich
        text and will happily grow wider than the screen rather than wrap. The
        grid holding the label is the only way to reach that width - the label
        has already been laid out by the time setText returns.
        """
        box = QMessageBox(self)
        box.setWindowTitle("Graphics workloads")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(body)
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setStandardButtons(QMessageBox.StandardButton.Close)

        spacer = QSpacerItem(HELP_WIDTH, 0, QSizePolicy.Policy.Minimum,
                             QSizePolicy.Policy.Expanding)
        layout = box.layout()
        layout.addItem(spacer, layout.rowCount(), 0, 1, layout.columnCount())
        return box

    def _selected_gpu(self):
        if not self.targets:
            return None
        key = self.gpu_choice.currentData()
        for target in self.targets:
            if target.key == key:
                return target
        return self.targets[0]

    def _select_workers(self, count):
        """Pick a worker count by value rather than by row."""
        index = self.cpu_workers.findData(count)
        if index >= 0:
            self.cpu_workers.setCurrentIndex(index)

    def _update_cpu_limit(self):
        """Keep the worker count within what the chosen pool actually offers."""
        pool = (self.topology.physical if self.cpu_physical.isChecked()
                else self.topology.logical)
        limit = max(1, len(pool))

        # Greying a row means reaching into the combo box's model: a
        # QComboBox has no per-item enable of its own.
        model = self.cpu_workers.model()
        for row in range(self.cpu_workers.count()):
            item = model.item(row)
            if item is not None:
                item.setEnabled(self.cpu_workers.itemData(row) <= limit)

        # A disabled row left current would still show its number in the closed
        # box and would still be what _config() reads back, so the selection
        # comes down with the limit.
        if (self.cpu_workers.currentData() or 1) > limit:
            self._select_workers(limit)

        self.cpu_summary.setText(
            "%d physical core(s), %d logical."
            % (len(self.topology.physical), len(self.topology.logical)))

    def _update_ram_warning(self):
        wanted = self.ram_gb.value() * GB
        self.ram_label.setText("%d GB" % self.ram_gb.value())
        if self._available and wanted > self._available:
            self.ram_note.setText(
                "⚠ More than the %.1f GB currently free. The kernel will "
                "start swapping, which makes the machine very slow and writes "
                "heavily to your disk. You will be asked to confirm."
                % (self._available / GB))
        else:
            self.ram_note.setText(
                "%.1f GB is free right now." % (self._available / GB)
                if self._available else "")

    def _config(self):
        target = self._selected_gpu()
        return stress.StressConfig(
            cpu_enabled=self.cpu_box.isChecked(),
            cpu_workers=self.cpu_workers.currentData() or 1,
            cpu_physical_only=self.cpu_physical.isChecked(),
            cpu_percent=self.cpu_percent.value(),
            ram_enabled=self.ram_box.isChecked(),
            ram_bytes=self.ram_gb.value() * GB,
            gpu_enabled=self.gpu_box.isChecked() and target is not None,
            gpu_key="" if target is None else target.key,
            gpu_percent=self.gpu_percent.value(),
            gpu_mode=self.gpu_mode.currentData() or stress.DEFAULT_GPU_MODE,
            vram_bytes=self.vram_gb.value() * GB,
        )

    # ---- running --------------------------------------------------------

    def start(self):
        if self.session is not None and self.session.running:
            return

        config = self._config()
        problems = config.problems(self.topology)
        if problems:
            QMessageBox.information(self, "Nothing to run", "\n".join(problems))
            return

        if config.ram_enabled and self._available and \
                config.ram_bytes > self._available:
            if not self._confirm_swap(config.ram_bytes):
                return

        if self.warn_choice.isChecked() and not self._confirm_no_limit():
            return

        self._peak = {}
        self._stopped_by_limit = False
        self._last_sample = time.monotonic()
        self.session = stress.StressSession(config, self.topology,
                                            self._selected_gpu())
        self.session.start()
        self._set_running(True)
        self._graph_hardware(config)
        log.info("Stress test started: %s", config)

    def _confirm_swap(self, wanted):
        box = QMessageBox(self)
        box.setWindowTitle("More memory than is free")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("You asked for %.1f GB, but only %.1f GB is free."
                    % (wanted / GB, self._available / GB))
        box.setInformativeText(
            "The kernel will page other programs out to disk to make room. The "
            "machine will become very slow while this runs, and it writes a "
            "lot to your drive.<br><br>Continue?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setEscapeButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _confirm_no_limit(self):
        box = QMessageBox(self)
        box.setWindowTitle("Run without a temperature limit?")
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText("The test will keep running however hot the hardware gets.")
        box.setInformativeText(
            "<b>Running at high temperatures can permanently damage your "
            "hardware.</b><br><br>"
            "Nothing will stop the test for you - watching the temperatures "
            "and stopping in time is entirely up to you. The author of this "
            "software accepts no responsibility for any damage that "
            "results.<br><br>Continue without a limit?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setEscapeButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def stop(self):
        if self.session is not None:
            self.session.stop()
            self.session = None
        self._set_running(False)

    def _set_running(self, running):
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

        # Settings are frozen while the load is on, then restored to whatever
        # they were - not simply enabled, since the graphics box may have been
        # unusable from the start for want of a card.
        for box in (self.cpu_box, self.ram_box, self.gpu_box):
            if running:
                self._was_enabled[box] = box.isEnabled()
                box.setEnabled(False)
            else:
                box.setEnabled(self._was_enabled.get(box, True))

        if not running and not self._stopped_by_limit:
            self.status.setText("Not running." + self._peak_summary())

    def _peak_summary(self):
        if not self._peak:
            return ""
        hottest = sorted(self._peak.values(), key=lambda lv: -lv[1])[:3]
        return "  Peak: " + ", ".join("%s %.0f °C" % (label, value)
                                      for label, value in hottest)

    # ---- graphs ---------------------------------------------------------

    @staticmethod
    def stressed_devices(config):
        """The device keys this test loads, in the order their graphs show."""
        devices = []
        if config.cpu_enabled:
            devices.append("cpu")
        if config.ram_enabled:
            devices.append("ram")
        if config.gpu_enabled and config.gpu_key:
            devices.append(config.gpu_key)
        return devices

    def _graph_hardware(self, config):
        """Open the graphs onto what this test loads.

        Replaces whatever was graphed before: a new test is a question about
        different hardware, and the strips should answer that one. Before the
        first reading has arrived there is nothing to choose from yet, so the
        choice waits for it.
        """
        self._graph_devices = self.stressed_devices(config)
        self._pick_pending = True
        self._pick_graphs()
        self.graphs_button.setChecked(True)

    def _pick_graphs(self):
        if not self._pick_pending or not self._models:
            return
        uids = series.stress_graphs(self._models, self._graph_devices)
        if uids:
            self._pick_pending = False
            self.graphs.set_selection(uids)

    def feed_graphs(self, models):
        """This tick's tree models, from the main window after it records."""
        self._models = list(models)
        self._pick_graphs()
        if self.graphs.isVisible():
            self.graphs.set_models(self._models)
            self.graphs.refresh(self.store)

    def set_graphs_visible(self, visible):
        """Show the graphs on this window's right, widening it to make room.

        Inside this window rather than in the main one's panel: on Wayland no
        window can be placed beside another, but a window can grow.
        """
        if visible == self.graphs.isVisible():
            return
        self.graphs_button.setText("Hide Graphs" if visible else "Show Graphs")
        resizable = not (self.isMaximized() or self.isFullScreen())
        if visible:
            before = self.width()
            self.graphs.show()
            if resizable:
                width = before + PANEL_WIDTH
                limits = screen_limits(self)
                if limits is not None:
                    width = min(width, limits[0])
                self.resize(width, self.height())
                self.layout().activate()
                self._graphs_widened = (before, self.width())
            if self._models:
                self.graphs.set_models(self._models)
                self.graphs.refresh(self.store)
        else:
            self.layout().activate()     # settle a resize still pending
            taken = self.graphs.width() + self.layout().spacing()
            self.graphs.hide()
            # Recomputed now: until it is, the hidden panel's minimum width
            # still holds the window open and the resize below is refused.
            self.layout().activate()
            if self._graphs_widened and resizable:
                self.resize(width_after_panel(self.width(), taken,
                                               self._graphs_widened),
                            self.height())
            self._graphs_widened = None

    # ---- live data ------------------------------------------------------

    @pyqtSlot(object)
    def take_sample(self, sample):
        """Temperatures from the main window's worker.

        Reused rather than read again here: the limit has to act on exactly the
        numbers the user is watching, and a second reader would drift from them.
        """
        temperatures = {}
        for reading in getattr(sample, "readings", ()):
            if reading.group == "Temperatures":
                temperatures[reading.uid] = (reading.label, reading.value)
                if reading.value > self._peak.get(reading.uid, (None, -273))[1]:
                    self._peak[reading.uid] = (reading.label, reading.value)
        if temperatures:
            self._temperatures = temperatures
            self._last_sample = time.monotonic()
        self._check_limit()

    def _readings_have_stopped(self):
        """Stop the test if the temperature limit has gone blind.

        Only matters when automatic stopping was chosen: that choice is the
        user's reason for trusting the test unattended, and it is worth nothing
        once readings stop arriving. Warn-only was already an informed decision
        to watch it themselves, so nothing is taken away there.
        """
        if not self.abort_choice.isChecked() or self._last_sample is None:
            return False
        if time.monotonic() - self._last_sample < SAMPLE_TIMEOUT_S:
            return False

        self._stopped_by_limit = True
        self.stop()
        self.status.setText(
            "Stopped: no temperature readings for %d seconds, so the %d °C "
            "limit could no longer be enforced.%s"
            % (SAMPLE_TIMEOUT_S, self.limit_spin.value(), self._peak_summary()))
        log.warning("Stress test stopped: sensor readings stopped arriving")
        return True

    def _check_limit(self):
        if self.session is None or not self.session.running:
            return
        if not self.abort_choice.isChecked():
            return
        limit = self.limit_spin.value()

        over = [(label, value) for label, value in self._temperatures.values()
                if value >= limit]
        if not over:
            return

        label, value = max(over, key=lambda kv: kv[1])
        self._stopped_by_limit = True
        self.stop()
        self.status.setText(
            "Stopped: %s reached %.0f °C, at or above the %d °C "
            "limit.%s" % (label, value, limit, self._peak_summary()))
        log.warning("Stress test stopped: %s at %.1f C (limit %d)",
                    label, value, limit)

    def _refresh(self):
        if self.session is None:
            return
        if self._readings_have_stopped():
            return
        if not self.session.running:
            # Every worker exited on its own - an allocation failed, or a child
            # was killed. Not silently left looking like it is still running.
            self.stop()
            return

        measured = None
        try:
            import psutil
            measured = psutil.cpu_percent(interval=None)
        except Exception:
            pass

        self.session.correct(_scaled_to_workers(measured, self.session,
                                                self.topology))
        state = self.session.status(measured)
        self.status.setText(self._describe(state))

    def _describe(self, state):
        bits = ["Running %s" % _duration(state.elapsed)]
        if state.cpu_requested:
            measured = ("%.0f %%" % state.cpu_measured
                        if state.cpu_measured is not None else "-")
            bits.append("CPU %d%% on %d core(s), total load %s"
                        % (state.cpu_requested, state.cpu_count, measured))
        if state.ram_requested:
            bits.append("Memory %.1f / %.1f GB committed"
                        % (state.ram_committed / GB, state.ram_requested / GB))
        if state.gpu_requested or state.vram_requested:
            piece = "GPU %d%%" % state.gpu_requested
            if state.gpu_measured is not None:
                piece += " (measured %d%%)" % state.gpu_measured
            if state.gpu_mode:
                piece += ", %s" % stress.gpu_mode_label(state.gpu_mode).lower()
            if state.vram_requested:
                piece += ", VRAM %.1f GB" % (state.vram_requested / GB)
            bits.append(piece)
        if self._temperatures:
            hottest = max(self._temperatures.values(), key=lambda lv: lv[1])
            bits.append("hottest %s %.0f °C" % hottest)
        text = "  |  ".join(bits)
        if state.notes:
            text += "\n" + "\n".join("⚠ " + note for note in state.notes)
        return text

    # ---- shutdown -------------------------------------------------------

    def is_finished(self):
        return self._closed

    def is_running(self):
        return self.session is not None and self.session.running

    def shutdown(self):
        """Stop everything. Safe to call more than once."""
        self._closed = True
        self._timer.stop()
        self.stop()

    def confirm_shutdown(self, parent=None):
        """False cancels the close. Asked only while a test is actually on."""
        if self._closed or not self.is_running():
            return True
        box = QMessageBox(parent or self)
        box.setWindowTitle("A stress test is running")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Stop the stress test and close?")
        box.setInformativeText(
            "The load ends as soon as you confirm; nothing is left running.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        return box.exec() == QMessageBox.StandardButton.Yes

    def closeEvent(self, event):
        if not self._closed and not self.confirm_shutdown():
            event.ignore()
            return
        # Saved without the graphs, which open by themselves with the next
        # test: a size that included them would reopen too wide and empty.
        self.graphs_button.setChecked(False)
        self._save_geometry()
        self.shutdown()
        super().closeEvent(event)

    # ---- geometry -------------------------------------------------------

    def _restore_geometry(self):
        saved = self.settings.value("stress_window_geometry")
        if saved is not None and hasattr(saved, "isEmpty") and not saved.isEmpty():
            self.restoreGeometry(saved)
            return
        self.resize(*fit_on_screen(self, 560, 620))

    def _save_geometry(self):
        try:
            self.settings.setValue("stress_window_geometry", self.saveGeometry())
        except Exception:
            log.exception("Could not save the stress window geometry")


def _scaled_to_workers(total_percent, session, topology):
    """Turn whole-machine CPU load into per-worker load.

    psutil reports the average across every CPU, but the request is per loaded
    core: four workers at 100% on a sixteen-thread machine is 25% overall and
    must not be corrected upward towards 100.
    """
    if total_percent is None or not session.config.cpu_enabled:
        return None
    loaded = len(session.loaded_cpus) or 1
    logical = len(topology.logical) or 1
    return min(100.0, total_percent * logical / loaded)


def _duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    return "%dm %02ds" % (seconds // 60, seconds % 60)
