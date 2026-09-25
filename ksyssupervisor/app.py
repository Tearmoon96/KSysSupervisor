"""Entry point: argument parsing, startup checks and the Qt application."""

import sys
import argparse
import traceback

from . import (APP_NAME, APP_VERSION, EXIT_GUI_INIT_FAILED, emergency_notify,
               fail_missing_module, log, log_file_path, setup_logging)
from .system import check_compatibility, check_dependencies


def ensure_dependencies():
    """Import the third-party dependencies, reporting clearly if any is absent.

    Nothing imports PyQt6 or psutil at module scope above this point, so a
    missing package produces a readable message instead of an ImportError
    traceback the user never sees when launched from the application menu.
    """
    try:
        import psutil                # noqa: F401
        import PyQt6.QtCore          # noqa: F401
        import PyQt6.QtGui           # noqa: F401
        import PyQt6.QtWidgets       # noqa: F401
    except ImportError as exc:       # pragma: no cover - depends on environment
        fail_missing_module(exc)


def install_excepthook():
    """Report uncaught exceptions instead of letting the app die silently."""
    def handler(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        log.critical("Unhandled exception:\n%s", detail)
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(
                None, "%s - unexpected error" % APP_NAME,
                "An unexpected error occurred.\n\nA full report was written to:\n%s\n\n%s"
                % (log_file_path(), detail))
        except Exception:
            emergency_notify("Unexpected error", detail)

    sys.excepthook = handler


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="ksyssupervisor",
        description="%s - a HWMonitor-style hardware monitor for Linux." % APP_NAME)
    parser.add_argument("--version", action="version",
                        version="%s %s" % (APP_NAME, APP_VERSION))
    parser.add_argument("--debug", action="store_true",
                        help="enable verbose logging")
    parser.add_argument("--no-dep-check", action="store_true",
                        help="skip the missing-utility check at startup")
    parser.add_argument("--interval", type=float, default=1.0, metavar="SECONDS",
                        help="sensor refresh interval in seconds (default: 1.0)")
    parser.add_argument("--install-fan-helper", action="store_true",
                        help="install the bundled fan helper system-wide "
                             "(asks for a password) and exit")
    # Unknown arguments are passed through to Qt (e.g. -style, -platform).
    return parser.parse_known_args(argv)


def install_fan_helper():
    """Handle --install-fan-helper: no window, just the pkexec prompt."""
    from .fanctl import install_bundled_helper

    ok, message = install_bundled_helper()
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    args, qt_args = parse_args(sys.argv[1:] if argv is None else argv)
    path = setup_logging(args.debug)
    log.info("Starting %s %s (log file: %s)", APP_NAME, APP_VERSION, path)

    check_compatibility()

    # Before the dependency check and before Qt: installing the helper needs
    # neither, and this is the one thing an AppImage user has to be able to do
    # from a terminal.
    if args.install_fan_helper:
        return install_fan_helper()

    ensure_dependencies()

    # Imported only now: check_compatibility() and ensure_dependencies() must be
    # able to report a problem before any Qt import can fail.
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from .window import KSysSupervisor

    # Named before the QApplication exists, not after. KDE's config is keyed
    # by the application name, and the platform theme opens it while the
    # QApplication is being built: renamed afterwards, the first theme icon
    # drawn left Breeze holding a second copy that a colour-scheme switch
    # never reloads, and the strip behind the menu bar stayed in the old
    # theme until the app was restarted. Also what lets the compositor match
    # the window to its .desktop entry for the icon and taskbar entry.
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationVersion(APP_VERSION)
    QApplication.setDesktopFileName("ksyssupervisor")

    try:
        app = QApplication([sys.argv[0]] + qt_args)
    except Exception as exc:
        log.critical("Qt application could not be created", exc_info=True)
        emergency_notify(
            "Could not start the interface",
            "Qt failed to initialise:\n\n%s\n\n"
            "On Wayland, check that the 'qt6-wayland' package is installed." % exc)
        return EXIT_GUI_INIT_FAILED

    # Set explicitly rather than left to the desktop file. That only supplies an
    # icon once something has installed one into the icon theme, which is not
    # true of a checkout or of an AppImage - and without it every window and the
    # task bar entry fall back to a generic placeholder.
    from .icons import app_icon
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    # Before any window reads its settings, and after QApplication exists so
    # QSettings has an organisation to key on.
    try:
        from .settings_migration import migrate
        migrate()
    except Exception:
        # Losing old preferences is a nuisance; failing to start over it would
        # be worse.
        log.exception("Could not carry settings over from the old name")

    install_excepthook()

    dep_report = ([], []) if args.no_dep_check else check_dependencies()

    try:
        window = KSysSupervisor(dep_report=dep_report)
        window.set_interval(args.interval)
        window.show()
    except Exception as exc:
        log.critical("The main window could not be built", exc_info=True)
        QMessageBox.critical(
            None, "%s - startup failed" % APP_NAME,
            "KSysSupervisor could not build its main window.\n\n%s\n\n"
            "Details were written to:\n%s\n\n%s"
            % (exc, log_file_path(), traceback.format_exc()))
        return EXIT_GUI_INIT_FAILED

    # Only warn once the event loop is running and the window is on screen, so
    # the dialog has a visible parent and can never block startup.
    QTimer.singleShot(0, window.warn_about_dependencies)
    # A few seconds in, so a slow network never delays the first readings.
    QTimer.singleShot(3000, window.updates.startup)

    return app.exec()
