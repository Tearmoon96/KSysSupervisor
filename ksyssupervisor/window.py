"""The main window: one of two views over the readings, refreshed on a timer.

The classic tree and the tile board render the same models from the same
builder and answer to the same handful of calls, so the window keeps both in
a stack and only ever talks to the one that is showing.
"""

import os
import sys
import logging
import platform

import psutil
from PyQt6.QtWidgets import (
    QMainWindow, QVBoxLayout, QWidget, QStackedWidget, QFileDialog,
    QMessageBox, QInputDialog, QCheckBox
)
from PyQt6.QtCore import (QSettings, QThread, QByteArray, QUrl,
                          QT_VERSION_STR, Qt, pyqtSignal)
from PyQt6.QtGui import QActionGroup, QIcon, QDesktopServices, QPalette

from . import APP_NAME, APP_VERSION, display, log, log_file_path
from .widgets import (THEME_EVENTS, TOOL_COLOURS, Backdrop, KeepOpenMenu,
                      Menu, explain, repolish, run_blocking)
from . import fanctl, hwmon, installation
from .display import TileBuilder
from .geometry import fit_on_screen, screen_limits
from .icons import fan_icon
from .providers import GROUP_ORDER, ProviderRegistry
from .livegraph import width_after_panel
from .series import DEFAULT_SPAN, SeriesStore
from .setupdialog import HardwareSetupDialog
from .tileboard import TileBoard
from .treeview import SensorTree
from .updatedialog import UpdateManager
from .worker import SensorWorker


# A thread that would not stop in time is kept alive here rather than being
# destroyed while running, which Qt treats as fatal.
_ORPHANED_THREADS = []

LAYOUT_TREE = "tree"
LAYOUT_TILES = "tiles"
LAYOUTS = (LAYOUT_TREE, LAYOUT_TILES)
DEFAULT_LAYOUT = LAYOUT_TREE

# A value the setting held briefly, for a tile layout that no longer exists
# on its own; it meant what "tiles" means now.
LAYOUT_ALIASES = {"tiles-scroll": LAYOUT_TILES}

LAYOUT_TITLES = {LAYOUT_TREE: "Classic Tree", LAYOUT_TILES: "Tiles"}

# The tree keeps the key it always had, so a size saved before the tiles
# existed still applies to it; the board's is its own, since the two want
# nothing like the same shape.
GEOMETRY_KEYS = {LAYOUT_TREE: "window_geometry",
                 LAYOUT_TILES: "window_geometry_tiles"}

# Wide enough for three columns of tiles, which is what the board is for;
# the tree opens narrow and widens itself to its columns once it has some.
DEFAULT_SIZES = {LAYOUT_TREE: (500, 700), LAYOUT_TILES: (1080, 760)}


#: What each group holds, for its entry in View > Categories.
CATEGORY_TIPS = {
    "Temperatures": "Show or hide every temperature, in both layouts.",
    "Utilization": "Show or hide load and usage: CPU and GPU load, memory "
                   "and drive space used, power against its limit.",
    "Clocks": "Show or hide clock speeds: every core, and the card's "
              "shader and memory clocks.",
    "Powers": "Show or hide power draw in watts.",
    "Voltages": "Show or hide voltage rails.",
    "Fans": "Show or hide fan speeds.",
    "VRAM": "Show or hide graphics memory.",
    "Charge": "Show or hide battery charge and health.",
}


class KSysSupervisor(QMainWindow):
    # Emitted to the worker thread; the timer lives there, not here.
    interval_requested = pyqtSignal(int)

    #: Ticks a sensor may miss before its value is blanked. Kept as an alias
    #: so the rule has one definition; display.Staleness enforces it.
    MISSING_GRACE = display.Staleness.GRACE

    def __init__(self, dep_report=None, settings=None):
        super().__init__()
        self.setWindowTitle(APP_NAME)

        # (missing, recommendations) from check_dependencies(), shown in Diagnostics
        self.dep_missing, self.dep_recommendations = dep_report or ([], [])

        # Non-fatal problems, keyed so each is reported once rather than every tick
        self.issues = {}
        self.backend = None
        self._dep_box = None
        self._read_failures = 0
        self._sensor_count = 0

        self.status = self.statusBar()

        # Passed in by tests, so they never touch the real preferences.
        self.settings = settings or QSettings("KSysSupervisor", "Settings")

        self.layout_name = self.settings.value("layout", DEFAULT_LAYOUT,
                                               type=str)
        self.layout_name = LAYOUT_ALIASES.get(self.layout_name,
                                              self.layout_name)
        if self.layout_name not in LAYOUTS:
            self.layout_name = DEFAULT_LAYOUT
        # The tree widens itself to its columns once, after the first sample;
        # a size the user chose is never overridden that way.
        self._fitted_to_columns = self._restore_geometry(self.layout_name)

        # Sensor plumbing. Providers discover the hardware and return flat
        # Readings; the views are built from those, so a device or group only
        # appears when something actually reports into it.
        self.registry = ProviderRegistry.default()
        self.advice = []
        self._interval_ms = 1000
        self._thread = None
        self._worker = None
        # Held so the fan window is not collected the moment show() returns;
        # it is parentless, so nothing else owns it.
        self._fan_window = None
        # Same reason as the fan window: parentless, so nothing else owns it.
        self._stress_window = None

        # The update check; started from app.py once the window is showing.
        self.updates = UpdateManager(self, self.settings)

        # Every reading's recent history, recorded from the start so a graph
        # opened later already shows the last few minutes.
        self.series = SeriesStore()
        self._graph_dock = None
        self._graph_panel = None
        # (width before, width after) the panel widened the window, so closing
        # it can put the window back; None when it did not widen it.
        self._graph_widened = None

        self.group_visible = {
            group: self.settings.value("show_cat_%s" % group, True, type=bool)
            for group in GROUP_ORDER
        }

        self.builder = TileBuilder(self.registry.devices())
        self.custom_names = self._load_custom_names()
        self.builder.configs = self._load_tile_configs()

        # Both views are built up front - they are cheap empty - but only the
        # one showing is fed; the other fills itself on the next switch.
        self.tree = SensorTree(self.settings)
        tree_page = QWidget()
        tree_layout = QVBoxLayout(tree_page)
        tree_layout.addWidget(self.tree)

        self.board = TileBoard(self.settings)
        tiles_page = Backdrop()
        tiles_layout = QVBoxLayout(tiles_page)
        tiles_layout.setContentsMargins(0, 0, 0, 0)
        tiles_layout.addWidget(self.board)

        self.views = {LAYOUT_TREE: self.tree, LAYOUT_TILES: self.board}
        self._pages = {LAYOUT_TREE: tree_page, LAYOUT_TILES: tiles_page}
        for view in self.views.values():
            view.rename_requested.connect(self.rename_device)
            view.reset_name_requested.connect(self.reset_device_name)
            view.clear_bounds_requested.connect(self.clear_min_max_for)
            view.order_changed.connect(self._save_order)
        self.tree.graph_requested.connect(self.graph_reading)
        self.board.graph_requested.connect(self.graph_reading)
        self.board.config_changed.connect(self.set_tile_config)
        self.board.name_changed.connect(self.set_device_name)

        self._stack = QStackedWidget()
        for name in LAYOUTS:
            self._stack.addWidget(self._pages[name])
        self._stack.setCurrentWidget(self._pages[self.layout_name])
        self.setCentralWidget(self._stack)

        # Views that have had the saved order applied: it can only be done
        # once each has something to order, which is after its first sync.
        self._ordered = set()

        self._setup_menubar()
        self._refresh_status()
        if self.settings.value("live_graphs/open", False, type=bool):
            # The saved window size already includes the panel: it was saved
            # while the panel was open, so it is shown without widening again.
            self.graph_action.setChecked(True)
        self._start_worker()

    @property
    def view(self):
        """Whichever of the two views is showing."""
        return self.views[self.layout_name]

    def _restore_geometry(self, layout_name):
        """Size the window for a layout. True when a saved size was used."""
        geometry = self.settings.value(GEOMETRY_KEYS[layout_name])
        if isinstance(geometry, QByteArray) and not geometry.isEmpty():
            self.restoreGeometry(geometry)
            return True
        self.resize(*fit_on_screen(self, *DEFAULT_SIZES[layout_name]))
        return False

    def _save_geometry(self, layout_name):
        try:
            self.settings.setValue(GEOMETRY_KEYS[layout_name],
                                   self.saveGeometry())
        except Exception:
            log.exception("Could not save the window geometry")

    def set_layout(self, layout_name):
        """Show the other view, sized the way it was last left."""
        if layout_name not in LAYOUTS or layout_name == self.layout_name:
            return
        self._save_geometry(self.layout_name)
        self.layout_name = layout_name
        self.settings.setValue("layout", layout_name)
        self._stack.setCurrentWidget(self._pages[layout_name])
        self._fitted_to_columns = self._restore_geometry(layout_name)
        for name, action in self.layout_actions.items():
            action.setChecked(name == layout_name)
        self._rebuild()

    # ---- tile customisation ---------------------------------------------

    TILE_CONFIG_GROUP = "tile_config"

    def _load_tile_configs(self):
        """What the user chose for each tile, by tile key."""
        configs = {}
        self.settings.beginGroup(self.TILE_CONFIG_GROUP)
        try:
            for key in self.settings.childKeys():
                config = display.TileConfig.from_json(
                    self.settings.value(key, "", type=str))
                if not config.is_default():
                    configs[key] = config
        finally:
            self.settings.endGroup()
        return configs

    def set_tile_config(self, key, config):
        """Keep a tile's new layout and show it straight away."""
        configs = self.builder.configs
        self.settings.beginGroup(self.TILE_CONFIG_GROUP)
        try:
            if config.is_default():
                configs.pop(key, None)
                self.settings.remove(key)
            else:
                configs[key] = config
                self.settings.setValue(key, config.to_json())
        finally:
            self.settings.endGroup()
        self._rebuild()

    def _load_custom_names(self):
        """Renames the user has saved, by device key.

        The lookup falls back to the display name once, to carry over
        settings from before categories were keyed by device.
        """
        names = {}
        for key, device in self.registry.devices().items():
            saved = (self.settings.value("custom_name_%s" % key, "", type=str)
                     or self.settings.value("custom_name_%s" % device.name, "",
                                            type=str))
            if saved:
                names[key] = saved
        # Tiles that stand for several devices have a key of their own, which
        # belongs to no provider and so is not in registry.devices().
        for merge_key, _title in display.MERGED.values():
            saved = self.settings.value("custom_name_%s" % merge_key, "",
                                        type=str)
            if saved:
                names[merge_key] = saved
        return names

    def _setup_menubar(self):
        menubar = self.menuBar()
        
        # File Menu
        file_menu = menubar.addMenu("File")
        save_action = file_menu.addAction(QIcon.fromTheme("document-save"), "Save Monitoring Data...")
        save_action.triggered.connect(self.save_data)
        explain(save_action, "Save Monitoring Data",
                "Write every reading, with its minimum and maximum since "
                "start, to a text file.")
        file_menu.addSeparator()
        exit_action = file_menu.addAction(QIcon.fromTheme("application-exit"), "Exit")
        exit_action.triggered.connect(self.close)
        explain(exit_action, "Exit",
                "Close KSysSupervisor. With a fan under manual control or a "
                "stress test running, you are asked what to do first.")

        # Edit Menu
        edit_menu = menubar.addMenu("Edit")
        clear_action = edit_menu.addAction(QIcon.fromTheme("edit-clear"), "Clear Min/Max")
        clear_action.triggered.connect(self.clear_min_max)
        explain(clear_action, "Clear Min/Max",
                "Forget the lowest and highest reading of every sensor and "
                "start counting again from now.")

        # View Menu
        view_menu = KeepOpenMenu("View", self)
        menubar.addMenu(view_menu)
        expand_action = view_menu.addAction(QIcon.fromTheme("zoom-in"),
                                            "Expand All")
        expand_action.triggered.connect(lambda: self.view.set_all_expanded(True))
        collapse_action = view_menu.addAction(QIcon.fromTheme("zoom-out"),
                                              "Collapse All")
        collapse_action.triggered.connect(
            lambda: self.view.set_all_expanded(False))
        explain(expand_action, "Expand All",
                "Open every category in the tree, or every More readings "
                "fold on the tiles.")
        explain(collapse_action, "Collapse All",
                "Close them all again.")

        view_menu.addSeparator()

        # Two exclusive entries rather than one toggle, so the menu says
        # which is showing and which is the other, not just "switch".
        layout_menu = Menu("Layout", view_menu)
        view_menu.addMenu(layout_menu)
        group = QActionGroup(self)
        group.setExclusive(True)
        self.layout_actions = {}
        for name in LAYOUTS:
            action = layout_menu.addAction(LAYOUT_TITLES[name])
            action.setCheckable(True)
            action.setChecked(name == self.layout_name)
            action.triggered.connect(
                lambda _checked, name=name: self.set_layout(name))
            group.addAction(action)
            self.layout_actions[name] = action
        explain(layout_menu.menuAction(), "Layout",
                "How the readings are laid out. Each layout keeps its own "
                "window size.")
        explain(self.layout_actions[LAYOUT_TREE], "Classic Tree",
                "Every device as a category and every reading as a row, "
                "with its minimum and maximum.")
        explain(self.layout_actions[LAYOUT_TILES], "Tiles",
                "A card per device with its main readings at a glance. "
                "Customize a card with its ✎ button.")

        view_menu.addSeparator()

        self.collapse_startup_action = view_menu.addAction("Collapse at Startup")
        self.collapse_startup_action.setCheckable(True)
        self.collapse_startup_action.setChecked(self.settings.value("collapse_startup", False, type=bool))
        self.collapse_startup_action.toggled.connect(self._toggle_collapse_startup)
        explain(self.collapse_startup_action, "Collapse at Startup",
                "Open the window with every category folded.")

        cat_menu = KeepOpenMenu("Categories", self)
        view_menu.addMenu(cat_menu)
        explain(cat_menu.menuAction(), "Categories",
                "Which kinds of reading are shown. The menu stays open so "
                "several can be switched at once.")
        for cat_name in GROUP_ORDER:
            action = cat_menu.addAction(cat_name)
            action.setCheckable(True)
            is_visible = self.settings.value(f"show_cat_{cat_name}", True, type=bool)
            action.setChecked(is_visible)
            action.toggled.connect(lambda checked, name=cat_name: self._toggle_category(name, checked))
            self._toggle_category(cat_name, is_visible)
            explain(action, cat_name, CATEGORY_TIPS.get(
                cat_name, "Show or hide these readings in both layouts."))

        # Tools Menu
        tools_menu = menubar.addMenu("Tools")
        # Drawn rather than themed: no common icon theme ships a fan, so this
        # was the one entry in the menu with a blank space beside it.
        self.fan_action = tools_menu.addAction(
            fan_icon(self.palette().color(QPalette.ColorRole.WindowText)),
            "Fan Control...")
        # Always clickable. It used to be greyed out whenever the helper was
        # missing, which left no way to find out why, let alone fix it; the
        # check now happens on click, where a missing or outdated helper can
        # be installed on the spot.
        self.fan_action.triggered.connect(self.show_fan_control)
        explain(self.fan_action, "Fan Control",
                "Set fan speeds by hand. Needs the small helper that writes "
                "to the fans as root; if it is missing this offers to "
                "install it.", TOOL_COLOURS["fans"])

        self.stress_action = tools_menu.addAction(
            QIcon.fromTheme("speedometer"), "Stress Test...")
        self.stress_action.triggered.connect(self.show_stress_test)
        explain(self.stress_action, "Stress Test",
                "Load the CPU, memory or graphics card to check cooling and "
                "stability, with a temperature limit that stops the test.",
                TOOL_COLOURS["stress"])

        self.graph_action = tools_menu.addAction(
            QIcon.fromTheme("office-chart-line",
                            QIcon.fromTheme("utilities-system-monitor")),
            "Live Graphs")
        self.graph_action.setCheckable(True)
        self.graph_action.toggled.connect(self.set_live_graphs_visible)
        explain(self.graph_action, "Live Graphs",
                "A panel beside the window graphing any readings you pick. "
                "Right-click a reading in the tree to add it.",
                TOOL_COLOURS["graphs"])

        # Help Menu
        help_menu = menubar.addMenu("Help")
        diag_action = help_menu.addAction(QIcon.fromTheme("help-about"), "Diagnostics...")
        diag_action.triggered.connect(self.show_diagnostics)
        explain(diag_action, "Diagnostics",
                "Versions, where the readings come from, and any problem "
                "found reading them - the thing to paste into a bug report.")
        self.setup_action = help_menu.addAction(
            QIcon.fromTheme("preferences-system"), "Hardware Setup...")
        self.setup_action.triggered.connect(self.show_hardware_setup)
        explain(self.setup_action, "Hardware Setup",
                "Step-by-step fixes for sensors that are missing: kernel "
                "modules to load and settings to change, with the commands.")
        help_menu.addSeparator()
        log_action = help_menu.addAction(QIcon.fromTheme("text-x-generic"), "Open Log File...")
        log_action.triggered.connect(self.open_log_file)
        explain(log_action, "Open Log File",
                "The app's own log, in your text editor.")

        help_menu.addSeparator()
        update_action = help_menu.addAction(
            QIcon.fromTheme("system-software-update"), "Check for Updates...")
        update_action.triggered.connect(self.updates.check_now)
        explain(update_action, "Check for Updates",
                "Ask GitHub whether a newer release is out, and offer to "
                "install it. Your settings are kept.")
        self.update_on_start_action = help_menu.addAction(
            "Check for Updates at Startup")
        self.update_on_start_action.setCheckable(True)
        self.update_on_start_action.setChecked(self.updates.check_on_start)
        self.update_on_start_action.toggled.connect(
            self.updates.set_check_on_start)
        explain(self.update_on_start_action, "Check at Startup",
                "Look for a new release a few seconds after the window opens. "
                "Nothing is sent but the request itself.")
        self.auto_update_action = help_menu.addAction(
            "Install Updates Automatically")
        self.auto_update_action.setCheckable(True)
        self.auto_update_action.setChecked(self.updates.auto_install)
        self.auto_update_action.setEnabled(self.updates.can_install)
        self.auto_update_action.toggled.connect(self.updates.set_auto_install)
        explain(self.auto_update_action, "Install Automatically",
                "When the startup check finds a new release, install it and "
                "restart without asking first. A system-wide installation "
                "still asks for your password."
                if self.updates.can_install else
                "Not available when running from a source folder: update it "
                "with git or download the release.")

    def _diagnostics_text(self):
        lines = [
            "%s %s" % (APP_NAME, APP_VERSION),
            "Python      : %s" % sys.version.split()[0],
            "Qt / PyQt6  : %s" % QT_VERSION_STR,
            "psutil      : %s" % getattr(psutil, "__version__", "unknown"),
            "Kernel      : %s (%s)" % (platform.release(), platform.machine()),
            "Session     : %s" % (os.environ.get("XDG_SESSION_TYPE") or "unknown"),
            "",
            "Sensor backend: %s" % (self.backend or "starting..."),
            "Sensors shown : %d" % self._sensor_count,
            "Log file      : %s" % log_file_path(),
            "Installation  : %s" % installation.kind(),
            "",
        ]

        if self.dep_missing:
            lines.append("Missing system utilities:")
            lines += ["  - %s" % m for m in self.dep_missing]
            lines.append("")
            lines.append("Recommendations:")
            lines += ["  %s" % r for r in self.dep_recommendations]
        else:
            lines.append("All optional system utilities were found.")
        lines.append("")

        if self.issues:
            lines.append("Problems encountered while reading sensors:")
            lines += ["  - %s" % msg for msg in self.issues.values()]
        else:
            lines.append("No sensor read errors so far.")
        lines.append("")

        lines.append("Hardware setup")
        lines.append("-" * 60)
        if self.advice:
            lines.append("Some sensors this machine has are not readable yet. "
                         "See Help > Hardware Setup")
            lines.append("for the commands, laid out properly.")
            lines.append("")
            for tip in self.advice:
                lines += [tip.as_text(), ""]
        else:
            lines.append("Everything this machine exposes is being read.")

        return "\n".join(lines)

    def show_diagnostics(self):
        # The full report goes to the log, so it stays available for
        # troubleshooting without a cramped details pane in the dialog.
        log.info("Diagnostics report:\n%s", self._diagnostics_text())

        box = QMessageBox(self)
        box.setWindowTitle("Diagnostics")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText("System and sensor status")
        box.setInformativeText(
            "Sensor backend: %s\nSensors shown: %d\nIssues: %d\n"
            "Hardware setup tips: %d\n\n"
            "The full report was written to the log (Help > Open Log File)."
            % (self.backend or "starting...", self._sensor_count,
               len(self.issues), len(self.advice)))

        # The tips carry shell commands, which are unreadable in a message box.
        setup = box.addButton("Hardware Setup...",
                              QMessageBox.ButtonRole.ActionRole)
        close = box.addButton(QMessageBox.StandardButton.Close)

        # Without an explicit escape button, Qt resolves the window's X to the
        # only other button there is - so closing the dialog opened the setup
        # window instead of dismissing it.
        box.setDefaultButton(close)
        box.setEscapeButton(close)

        box.exec()
        if box.clickedButton() is setup:
            self.show_hardware_setup()

    def show_hardware_setup(self):
        """Open the setup tips in a window big enough to read the commands."""
        dialog = HardwareSetupDialog(self.advice, self.settings, self)
        dialog.exec()

    def show_stress_test(self):
        """Open the stress test window, or raise the one already open."""
        from .stresswindow import StressWindow

        if self._stress_window is None or self._stress_window.is_finished():
            self._stress_window = StressWindow(self.settings, self.series)
            # The limit has to act on the same numbers shown in the tree, so
            # the window is fed from this worker rather than reading sysfs
            # again on its own.
            if self._worker is not None:
                self._worker.sample_ready.connect(
                    self._stress_window.take_sample)
        self._stress_window.show()
        self._stress_window.raise_()
        self._stress_window.activateWindow()

    # ---- live graphs ----------------------------------------------------

    def set_live_graphs_visible(self, visible):
        """Show or hide the graph panel, on the window's right edge.

        A dock rather than a window of its own: on Wayland an application
        cannot place its windows, so only a dock can be kept at the main
        window's side. The window grows by the panel's width when it opens,
        so the sensor view is not squeezed to make room, and gives that width
        back when it closes.
        """
        if visible:
            restoring = self._graph_dock is None and not self.isVisible()
            self._ensure_graph_dock()
            self._graph_dock.show()
            if restoring:
                # Opened from the saved state: that size already has room, so
                # closing gives back only what the panel occupies.
                self._graph_widened = (None, None)
            elif not self._graph_dock.isFloating():
                self._widen_for_graphs()
            self._feed_graphs()
        elif self._graph_dock is not None:
            taken = self._graph_extent()
            self._graph_dock.hide()
            self._shrink_after_graphs(taken)
        self.settings.setValue("live_graphs/open", bool(visible))

    def _ensure_graph_dock(self):
        if self._graph_dock is not None:
            return
        from .livegraph import LiveGraphDock, LiveGraphPanel

        saved = self.settings.value("live_graphs/metrics", [], type=list)
        span = self.settings.value("live_graphs/span", DEFAULT_SPAN, type=int)
        self._graph_panel = LiveGraphPanel([str(u) for u in saved or []], span)
        self._graph_panel.selection_changed.connect(self._save_graph_selection)
        self._graph_panel.span_changed.connect(self._graph_span_changed)
        self._graph_dock = LiveGraphDock(self._graph_panel, self)
        self._graph_dock.closed.connect(
            lambda: self.graph_action.setChecked(False))
        self._graph_dock.topLevelChanged.connect(self._graph_dock_floated)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea,
                           self._graph_dock)

    def _widen_for_graphs(self):
        if self._graph_widened or self.isMaximized() or self.isFullScreen():
            return
        wanted = self._graph_dock.sizeHint().width()
        before = self.width()
        width = before + wanted
        limits = screen_limits(self)
        if limits is not None:
            width = min(width, limits[0])
        self.resize(width, self.height())
        self.resizeDocks([self._graph_dock], [wanted],
                         Qt.Orientation.Horizontal)
        self.layout().activate()
        self._graph_widened = (before, self.width())

    def _graph_extent(self):
        """The width the docked panel occupies, splitter included."""
        self.layout().activate()         # settle a resize still pending
        return self.width() - self.centralWidget().width()

    def _shrink_after_graphs(self, taken=None):
        """Give the panel's width back; see width_after_panel()."""
        if taken is None:
            taken = self._graph_extent()
        if self._graph_widened and not (self.isMaximized()
                                        or self.isFullScreen()):
            self.layout().activate()
            self.resize(max(self.minimumWidth(),
                            width_after_panel(self.width(), taken,
                                               self._graph_widened)),
                        self.height())
        self._graph_widened = None

    def _graph_dock_floated(self, floating):
        """A floating panel takes no room here; a re-docked one does again."""
        if not self._graph_dock.isVisible():
            return
        if floating:
            self._shrink_after_graphs()
        else:
            self._widen_for_graphs()

    def _save_graph_selection(self, uids):
        self.settings.setValue("live_graphs/metrics", list(uids))
        self._feed_graphs()

    def _graph_span_changed(self, span):
        self.settings.setValue("live_graphs/span", int(span))
        self._feed_graphs()

    def graph_reading(self, uid):
        """Add one reading to the graphs, opening the panel if it is shut."""
        self.graph_action.setChecked(True)
        self._graph_panel.add(uid)

    def _feed_graphs(self):
        """Hand the panel this tick's names and history, if it is showing.

        Every category, hidden ones included: hiding a group in the tree is a
        choice about the tree, and a graph already chosen should not vanish.
        """
        panel = (self._graph_panel is not None
                 and self._graph_dock.isVisible())
        # The stress window chooses its graphs from these too, so it is fed
        # whenever it is open, not only while its graphs are showing.
        stress = (self._stress_window is not None
                  and not self._stress_window.is_finished()
                  and self._stress_window.isVisible())
        if not (panel or stress):
            return
        models = self.builder.tree(names=self.custom_names)
        if panel:
            self._graph_panel.set_models(models)
            self._graph_panel.refresh(self.series)
        if stress:
            self._stress_window.feed_graphs(models)

    def show_fan_control(self):
        """Open the fan control window, or raise the one already open.

        Imported here rather than at module scope so the fan control UI is only
        built when it is actually asked for.
        """
        from .fanwindow import FanControlWindow

        state, reason = fanctl.availability()
        if state != fanctl.READY and not self._make_fan_control_ready(state,
                                                                      reason):
            return

        # A closed window has already handed the helper back, so reopening
        # means a new session and a new authorisation, not reviving that one.
        if self._fan_window is None or self._fan_window.is_finished():
            self._fan_window = FanControlWindow(self.settings)
        self._fan_window.show()
        self._fan_window.raise_()
        self._fan_window.activateWindow()

    def _make_fan_control_ready(self, state, reason):
        """Fix what can be fixed from here. True once fan control can run.

        A missing or outdated helper is the one problem the app can solve by
        itself: it installs its own copy of the helper system-wide, which takes
        one password prompt and nothing else. The application is not
        reinstalled or moved - a per-user install stays per-user - and no
        setting is touched. Anything else needs the user, so it is explained.
        """
        if not fanctl.can_install_helper(state):
            QMessageBox.information(self, "Fan control unavailable", reason)
            return False

        updating = state == fanctl.OUTDATED_HELPER
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Fan control")
        if updating:
            box.setText("The fan control helper needs updating.")
            box.setInformativeText(
                "The installed copy is from an older version of %s. Update "
                "it now? You will be asked for your password once.\n\n"
                "Only the helper is replaced. %s itself stays where it is "
                "installed, and your settings are not changed."
                % (APP_NAME, APP_NAME))
            label = "Update Helper"
        else:
            box.setText("Fan control needs a small helper installed.")
            box.setInformativeText(
                "Setting a fan speed needs root, so a small helper is "
                "installed system-wide at %s. Install it now? You will be "
                "asked for your password once.\n\n"
                "Only the helper is installed. %s itself stays where it is - "
                "a per-user installation stays per-user - and your settings "
                "are not changed."
                % (os.path.dirname(fanctl.HELPER_INSTALL_PATH), APP_NAME))
            label = "Install Helper"
        install = box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(install)
        box.exec()
        if box.clickedButton() is not install:
            return False

        self.fan_action.setEnabled(False)
        self.status.showMessage("Waiting for authorisation...")
        try:
            (ok, message), error = self._install_helper()
        finally:
            self.fan_action.setEnabled(True)
            self.status.clearMessage()
        if error is not None:
            log.error("Installing the fan helper failed: %r", error)
            ok, message = False, str(error)
        if not ok:
            QMessageBox.warning(self, "Fan control helper", message)
            return False

        state, reason = fanctl.availability()
        if state != fanctl.READY:
            QMessageBox.warning(self, "Fan control unavailable", reason)
            return False
        log.info("Fan control helper installed from the application")
        return True

    @staticmethod
    def _install_helper():
        """install_bundled_helper() off the GUI thread: it waits on pkexec."""
        result, error = run_blocking(fanctl.install_bundled_helper)
        return (result or (False, "")), error

    def open_log_file(self):
        path = log_file_path()
        if not os.path.exists(path):
            QMessageBox.information(
                self, "No log file",
                "No log file has been written yet.\n\nIt will appear at:\n%s" % path)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            QMessageBox.information(self, "Log file location",
                                    "The log file is at:\n\n%s" % path)

    def warn_about_dependencies(self):
        """Warn about missing utilities, after the window is already visible.

        This used to run as a modal dialog before any window existed, which on
        Wayland could leave the app blocked on a dialog the user never saw.
        """
        if not self.dep_missing:
            return
        if self.settings.value("hide_dep_warning", False, type=bool):
            log.info("Missing utilities (warning suppressed): %s", ", ".join(self.dep_missing))
            return

        msg = "These optional system utilities are missing:\n\n"
        msg += "".join("  - %s\n" % m for m in self.dep_missing)
        msg += "\nRecommendations based on your hardware:\n"
        msg += "".join("%s\n" % r for r in self.dep_recommendations)
        msg += ("\nKSysSupervisor will keep running and reads what it can directly from "
                "/sys/class/hwmon, but some readings may be missing or less accurate.")

        box = QMessageBox(self)
        box.setWindowTitle("Missing System Dependencies")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(msg)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        checkbox = QCheckBox("Don't show this again")
        box.setCheckBox(checkbox)

        # open() rather than exec(): the main window stays usable, and a dialog
        # the compositor fails to present can never block the app the way the
        # old pre-window exec() did.
        self._dep_box = box
        box.finished.connect(lambda _result: self._dependency_warning_closed(checkbox))
        box.open()

    def _dependency_warning_closed(self, checkbox):
        if checkbox.isChecked():
            self.settings.setValue("hide_dep_warning", True)
        self._dep_box = None

    def _default_name(self, key):
        device = self.registry.device(key)
        if device is not None:
            return device.name
        for merge_key, title in display.MERGED.values():
            if merge_key == key:
                return title
        return key

    def rename_device(self, key):
        current = self.custom_names.get(key) or self._default_name(key)
        new_name, ok = QInputDialog.getText(self, "Rename", "Enter new name:",
                                            text=current)
        if not ok or not new_name.strip():
            return
        self.set_device_name(key, new_name)

    def set_device_name(self, key, name):
        """Call a device `name` everywhere; "" or its own name resets it."""
        name = (name or "").strip()
        if not name or name == self._default_name(key):
            self.reset_device_name(key)
            return
        self.custom_names[key] = name
        self.settings.setValue("custom_name_%s" % key, name)
        self._rebuild()

    def reset_device_name(self, key):
        self.custom_names.pop(key, None)
        self.settings.remove("custom_name_%s" % key)
        # Early pre-release settings were keyed by display name, and a reset that
        # left one of those behind would be undone on the next start.
        self.settings.remove("custom_name_%s" % self._default_name(key))
        self._rebuild()

    def _toggle_collapse_startup(self, checked):
        self.settings.setValue("collapse_startup", checked)

    def _toggle_category(self, cat_name, visible):
        self.settings.setValue(f"show_cat_{cat_name}", visible)
        self.group_visible[cat_name] = visible
        self._rebuild()

    def clear_min_max(self):
        self.builder.clear_bounds()
        self._rebuild()

    def clear_min_max_for(self, key):
        """Clear the range on one device, from its context menu.

        Walked from the tree models rather than the tiles: the drives share
        one tile, and "here" on a single drive should mean that drive.
        """
        keys = {key}
        for merge_key, _title in display.MERGED.values():
            if merge_key == key:
                keys.update(d.key for d in self.registry.devices().values()
                            if d.order in display.MERGED
                            and display.MERGED[d.order][0] == merge_key)
        for model in self.builder.tree():
            if model.key not in keys:
                continue
            for _group, metrics in model.groups:
                for metric in metrics:
                    self.builder.history.forget(metric.uid)
        self._rebuild()

    def _rebuild(self):
        """Re-render from the readings already in hand, with no new sample."""
        if not self.builder.known():
            return
        self._sync_view()

    def _sync_view(self):
        """Feed the view that is showing; the other one waits its turn."""
        names, visible = self.custom_names, self.group_visible
        if self.layout_name == LAYOUT_TREE:
            self.tree.sync(self.builder.tree(names=names, group_visible=visible))
        else:
            self.view.sync(self.builder.rebuild(names=names,
                                                group_visible=visible))
        self._after_first_content()

    def save_data(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Monitoring Data", "ksyssupervisor_export.txt", "Text Files (*.txt);;All Files (*)")
        if not path:
            return

        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("KSysSupervisor Export\n")
                f.write("=" * 40 + "\n")
                # The flat listing, whichever view is showing: every reading
                # once, no summaries, no drives folded together.
                for model in self.builder.tree(names=self.custom_names,
                                               group_visible=self.group_visible):
                    f.write("\n%s\n" % model.name)
                    f.write("-" * len(model.name) + "\n")
                    for group, metrics in model.groups:
                        f.write("  [%s]\n" % group)
                        for metric in metrics:
                            self._write_metric(f, metric)

            QMessageBox.information(self, "Success", "Monitoring data saved successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save data:\n{e}")

    @staticmethod
    def _write_metric(handle, metric):
        line = "%-28s %-16s" % (metric.label, metric.text)
        if metric.low is not None and not metric.total:
            line += " Min: %-14s Max: %s" % (
                display.format_value(metric.low, metric.kind),
                display.format_value(metric.high, metric.kind))
        handle.write(line.rstrip() + "\n")

    # ---- status reporting -----------------------------------------------

    def _set_backend(self, backend):
        if backend != self.backend:
            self.backend = backend
            log.info("Sensor backend in use: %s", backend)
            self._refresh_status()

    def _report_issue(self, key, message, level=logging.WARNING):
        """Record a non-fatal problem once, rather than every second."""
        if key in self.issues:
            return
        self.issues[key] = message
        log.log(level, message)
        self._refresh_status()

    def _refresh_status(self):
        text = {
            "lm_sensors": "Sensors: lm_sensors",
            "sysfs": "Sensors: /sys/class/hwmon (lm_sensors not installed)",
            "none": "Sensors: unavailable",
        }.get(self.backend, "Sensors: starting...")
        if self.issues:
            text += "    -    %d issue(s)" % len(self.issues)
        if self.advice:
            text += "    -    %d hardware setup tip(s): Help > Hardware Setup" \
                % len(self.advice)
        elif self.issues:
            text += " - see Help > Diagnostics"
        self.status.showMessage(text)

    # ---- the views ------------------------------------------------------

    def _save_order(self, keys):
        try:
            self.settings.setValue("category_order", list(keys))
        except Exception:
            log.exception("Could not persist the device order")

    def _after_first_content(self):
        """What can only be done once a view has something in it.

        The saved order and the collapse-at-startup choice are applied the
        first time each view is filled - there is nothing to order or fold
        before then - and the tree widens the window to its columns once.
        """
        view = self.view
        if view not in self._ordered:
            self._ordered.add(view)
            saved = self.settings.value("category_order", [], type=list)
            if saved:
                view.apply_order(list(saved))
            if self.settings.value("collapse_startup", False, type=bool):
                view.set_all_expanded(False)
        if self.layout_name == LAYOUT_TREE and not self._fitted_to_columns:
            self._fitted_to_columns = True
            wanted = self.tree.wanted_width(self)
            if wanted is not None and wanted > self.width():
                self.resize(wanted, self.height())

    # ---- sensor updates -------------------------------------------------

    def _start_worker(self):
        self._thread = QThread(self)
        self._worker = SensorWorker(self.registry, self._interval_ms)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.begin)
        self._worker.sample_ready.connect(self._apply_sample)
        # A worker that is replaced must not leave the stress window blind: its
        # temperature limit is only a safeguard while readings keep arriving.
        if self._stress_window is not None and not self._stress_window.is_finished():
            self._worker.sample_ready.connect(self._stress_window.take_sample)
        self._worker.failed.connect(self._on_read_failure)
        self.interval_requested.connect(self._worker.set_interval)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _stop_worker(self, kill_commands=False):
        """Stop the sensor thread without ever outliving it.

        Qt aborts the process if a running QThread is destroyed, and the window
        owns this one, so closing mid-read used to be a race against a five
        second subprocess timeout.
        """
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None
        if thread is None:
            return

        if worker is not None:
            # No late sample may reach a tree that is being torn down.
            for signal in (worker.sample_ready, worker.failed):
                try:
                    signal.disconnect()
                except TypeError:
                    pass

        if kill_commands:
            hwmon.shutdown()

        thread.quit()
        if thread.wait(3000):
            return

        # Still stuck: hand it to Qt to clean up on its own, and make sure the
        # window's destruction cannot take a live thread down with it.
        log.warning("The sensor thread did not stop in time; detaching it.")
        thread.setParent(None)
        thread.finished.connect(thread.deleteLater)
        _ORPHANED_THREADS.append(thread)

    def _apply_sample(self, sample):
        """Fold one worker Sample into the board. Runs on the GUI thread."""
        self._read_failures = 0
        self._set_backend(sample.backend)

        for key, message in sample.issues.items():
            self._report_issue(key, message)

        if sample.advice != self.advice:
            self.advice = list(sample.advice)
            self._refresh_status()

        self._sensor_count = len(sample.readings)
        self.series.record(sample.readings)
        # build() records the tick - staleness, first-seen order, min/max -
        # and what it returns is the tiles' render, which is only shown when
        # that is the view; the tree is rendered from the same state.
        tiles = self.builder.build(sample.readings, names=self.custom_names,
                                   group_visible=self.group_visible)
        if self.layout_name == LAYOUT_TREE:
            self._sync_view()
        else:
            self.view.sync(tiles)
            self._after_first_content()
        self._feed_graphs()

    def _on_read_failure(self, detail):
        """A whole read pass raised.

        Individual providers are already guarded, so reaching here means
        something broke in the registry itself.
        """
        self._read_failures += 1
        log.error("Sensor update failed (failure %d)\n%s",
                  self._read_failures, detail)

        if self._read_failures >= 5 and "update-loop" not in self.issues:
            self._stop_worker()
            self._report_issue("update-loop",
                               "Sensor updates stopped after repeated errors.")
            QMessageBox.critical(
                self, "Sensor updates stopped",
                "KSysSupervisor hit repeated errors while reading sensors and has "
                "stopped updating.\n\nThe window stays open so you can still read "
                "the last values.\n\nDetails have been written to:\n%s\n\n%s"
                % (log_file_path(), detail))

    def changeEvent(self, event):
        """Follow a light/dark switch.

        The tiles repaint themselves from the palette. Two things do not:
        the fan icon in the Tools menu, drawn once with a colour passed in,
        and under KDE's Breeze the menu bar itself, which kept the old
        theme's text colour until the window was reopened. The bar and its
        menus are repolished so the style reads the new palette.
        """
        if event.type() in THEME_EVENTS:
            self.fan_action.setIcon(
                fan_icon(self.palette().color(QPalette.ColorRole.WindowText)))
            menubar = self.menuBar()
            repolish(menubar, self.statusBar(),
                     *[action.menu() for action in menubar.actions()])
        super().changeEvent(event)

    def set_interval(self, seconds):
        """Set the sensor refresh interval, clamped to something sane."""
        self._interval_ms = max(200, int(seconds * 1000))
        self.interval_requested.emit(self._interval_ms)

    def close_tools(self):
        """Stop the stress test and hand the fans back. False if cancelled.

        Both questions are asked before anything is torn down: the user may
        still cancel, and by then stopping anything would be wrong. Used on
        the way out and before an update replaces the files under them.
        """
        if self._stress_window is not None:
            if not self._stress_window.confirm_shutdown(self):
                return False
        if self._fan_window is not None:
            if not self._fan_window.confirm_shutdown(self):
                return False

        if self._stress_window is not None:
            self._stress_window.shutdown()
            self._stress_window.close()
            self._stress_window = None
        if self._fan_window is not None:
            self._fan_window.shutdown()
            self._fan_window.close()
            self._fan_window = None
        return True

    def closeEvent(self, event):
        if not self.close_tools():
            event.ignore()
            return

        self._stop_worker(kill_commands=True)
        self._save_geometry(self.layout_name)
        super().closeEvent(event)
