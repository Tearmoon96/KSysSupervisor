"""KSysSupervisor - a HWMonitor-style hardware monitor for Linux.

Core constants, logging and pre-Qt failure reporting.

This module deliberately imports nothing outside the standard library: it is
loaded before the dependency check runs, so that a missing psutil or PyQt6 can
still be reported to the user rather than dying as a bare ImportError.
"""

import os
import sys
import shutil
import logging
import subprocess


APP_NAME = "KSysSupervisor"
#: The one place the version lives. The release script, the AppImage build,
#: --version, Diagnostics and the update check all read it from here; a release
#: is tagged "v" + this.
APP_VERSION = "1.0.0"

#: The GitHub repository releases are published to, as "owner/name". The update
#: check asks this repository for its latest release.
GITHUB_REPO = "Tearmoon96/KSysSupervisor"

# Distinct exit codes so a failed launch is diagnosable from a terminal or
# from the systemd/journal output of a .desktop launch.
EXIT_OK = 0
EXIT_UNSUPPORTED_PLATFORM = 2
EXIT_MISSING_PYTHON_DEPS = 3
EXIT_NO_DISPLAY = 4
EXIT_GUI_INIT_FAILED = 5

log = logging.getLogger(APP_NAME)


def log_file_path():
    """Path of the persistent log file (XDG state dir)."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, APP_NAME, "ksyssupervisor.log")


def setup_logging(debug=False):
    """Log to stderr and to a file.

    The app is normally started from a .desktop entry with Terminal=false, so
    stderr goes nowhere the user can see. The log file is the only durable
    record of why a launch failed.
    """
    level = logging.DEBUG if debug else logging.INFO
    log.setLevel(level)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    path = log_file_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Keep the log from growing without bound; this runs on a 1s timer.
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.replace(path, path + ".1")
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(fmt)
        log.addHandler(handler)
    except OSError as exc:
        log.warning("Could not open log file %s: %s", path, exc)

    return path


def emergency_notify(title, message):
    """Show a message when Qt is unavailable or has not started yet.

    Falls back through the desktop dialog helpers, then a notification, then
    stderr. Without this a launch failure from the application menu is
    completely silent.
    """
    sys.stderr.write("\n%s: %s\n%s\n" % (APP_NAME, title, message))
    sys.stderr.flush()

    for argv in (
        ["kdialog", "--title", title, "--error", message],
        ["zenity", "--error", "--title", title, "--text", message],
        ["notify-send", "--urgency=critical", "%s: %s" % (APP_NAME, title), message],
    ):
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, timeout=120, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except (OSError, subprocess.SubprocessError):
                continue


def fail_missing_module(exc):
    module = getattr(exc, "name", None) or "a required module"
    emergency_notify(
        "Missing Python dependency",
        "%s could not start because the Python module '%s' is not installed.\n\n"
        "Install them from your distribution:\n"
        "    Fedora:         sudo dnf install python3-psutil python3-pyqt6\n"
        "    Debian/Ubuntu:  sudo apt install python3-psutil python3-pyqt6\n"
        "    Arch:           sudo pacman -S python-psutil python-pyqt6\n\n"
        "or with pip:\n"
        "    pip install --user -r requirements.txt\n\n"
        "Original error: %s" % (APP_NAME, module, exc),
    )
    sys.exit(EXIT_MISSING_PYTHON_DEPS)


def resource_path(relative_path):
    """Absolute path to a bundled resource, in dev and under PyInstaller.

    The fallback resolves against the project directory rather than the
    working directory: launched from the menu the app starts in $HOME, and
    resolving there would look for resources somewhere they never are.
    """
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path is None:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)
