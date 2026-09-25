"""Where this copy of KSysSupervisor lives, and how it was put there.

Three ways of running it exist, and several features need to know which one
this is: the fan helper hints name the command that runs *this* copy, and the
updater replaces the copy in whatever way it was installed.

- an AppImage, whose runtime sets $APPIMAGE and $APPDIR;
- an installation made by install.sh, per user or system-wide;
- a plain source checkout run with `python3 KSysSupervisor.py`.

Standard library only, and no Qt: it is used before Qt is imported and by the
tests without a display.
"""

import os
import shutil

APPIMAGE = "appimage"
USER = "user"
SYSTEM = "system"
SOURCE = "source"

#: Where `install.sh --system` puts the application and its launcher.
SYSTEM_DIR = "/opt/ksyssupervisor"
SYSTEM_LAUNCHER = "/usr/local/bin/ksyssupervisor"

#: The directory holding KSysSupervisor.py and the ksyssupervisor package.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _home(env):
    return env.get("HOME") or os.path.expanduser("~")


def user_dir(env=None):
    """Where `install.sh --user` puts the application."""
    env = os.environ if env is None else env
    data = env.get("XDG_DATA_HOME") or os.path.join(_home(env), ".local", "share")
    return os.path.join(data, "ksyssupervisor")


def user_launcher(env=None):
    env = os.environ if env is None else env
    return os.path.join(_home(env), ".local", "bin", "ksyssupervisor")


def _same(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


def _inside(path, parent):
    try:
        path, parent = os.path.realpath(path), os.path.realpath(parent)
    except (OSError, ValueError):
        return False
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def kind(env=None, app_dir=None):
    """APPIMAGE, USER, SYSTEM or SOURCE.

    $APPIMAGE alone is not trusted: an AppImage passes it on to everything it
    starts, so a copy launched from inside another AppImage would otherwise
    take itself for that AppImage. The package also has to live under $APPDIR.
    """
    env = os.environ if env is None else env
    app_dir = APP_DIR if app_dir is None else app_dir

    appimage, appdir = env.get("APPIMAGE"), env.get("APPDIR")
    if appimage and appdir and os.path.isfile(appimage) and _inside(app_dir, appdir):
        return APPIMAGE
    if _same(app_dir, user_dir(env)):
        return USER
    if _same(app_dir, SYSTEM_DIR):
        return SYSTEM
    return SOURCE


def launcher(install_kind, env=None):
    """The command that starts the installed copy, or None for a checkout."""
    env = os.environ if env is None else env
    if install_kind == APPIMAGE:
        return env.get("APPIMAGE")
    if install_kind == USER:
        return user_launcher(env)
    if install_kind == SYSTEM:
        return SYSTEM_LAUNCHER
    return None


def self_command(env=None, app_dir=None):
    """How to run this copy from a terminal, for messages that tell the user to.

    The short name only where it really resolves to this installation: an
    install.sh per-user launcher is not on PATH everywhere.
    """
    env = os.environ if env is None else env
    app_dir = APP_DIR if app_dir is None else app_dir
    current = kind(env, app_dir)
    path = launcher(current, env)
    if current in (USER, SYSTEM) and path:
        found = shutil.which("ksyssupervisor", path=env.get("PATH"))
        if found and _same(found, path):
            return "ksyssupervisor"
        return path
    if current == APPIMAGE and path:
        return path
    return "python3 %s" % os.path.join(app_dir, "KSysSupervisor.py")
