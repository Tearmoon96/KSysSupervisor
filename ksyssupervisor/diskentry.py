"""The desktop's right-click entry for the disk utility.

Plasma offers "actions" for a storage device - in the Places panel's context
menu and in the Device Notifier - from small .desktop files under
solid/actions. One of them makes "KSysSupervisor Disk Utility" appear there,
starting this application straight into the disk utility on that device.

install.sh puts the entry in place for an installed copy. An AppImage has no
installer, so the window offers a switch that writes the same entry for the
current user; because the entry names the AppImage's path, and an update
renames the AppImage, it is checked at every start and rewritten when stale.

Standard library only.
"""

from __future__ import annotations

import os
import sys

from . import helperbase, installation, log

ENTRY_NAME = "ksyssupervisor_diskutility.desktop"
SYSTEM_ENTRY = os.path.join("/usr/share/solid/actions", ENTRY_NAME)
PLACEHOLDER = "@EXEC@"

USER = "user"
SYSTEM = "system"


def template_path(root=None):
    return os.path.join(helperbase.APP_ROOT if root is None else root,
                        "packaging", ENTRY_NAME)


def user_entry(env=None):
    env = os.environ if env is None else env
    data = env.get("XDG_DATA_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".local", "share")
    return os.path.join(data, "solid", "actions", ENTRY_NAME)


def command(env=None, app_dir=None):
    """The words that start this copy, as a list.

    A list rather than installation.self_command()'s one string, because it
    goes into an Exec line where each word has to be quoted on its own.
    """
    env = os.environ if env is None else env
    kind = installation.kind(env, app_dir)
    launcher = installation.launcher(kind, env)
    if launcher:
        return [launcher]
    app_dir = installation.APP_DIR if app_dir is None else app_dir
    return [sys.executable or "python3",
            os.path.join(app_dir, "KSysSupervisor.py")]


def quote(word):
    """One word of an Exec line, quoted by the desktop entry rules.

    Quoted whenever it holds anything but plain characters; inside the
    quotes a double quote, backtick, dollar sign and backslash are escaped,
    and a percent sign is doubled so it is not read as a field code.
    """
    word = word.replace("%", "%%")
    plain = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789-_./+@:,=%")
    if word and all(c in plain for c in word):
        return word
    for special in ("\\", '"', "`", "$"):
        word = word.replace(special, "\\" + special)
    return '"%s"' % word


def render(words=None, root=None):
    """The entry's text for a copy started by `words`, or None if this copy
    ships no template."""
    try:
        with open(template_path(root), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    words = command() if words is None else words
    return text.replace(PLACEHOLDER, " ".join(quote(w) for w in words))


def installed(env=None):
    """USER, SYSTEM or None: where the entry is, if anywhere."""
    if os.path.isfile(user_entry(env)):
        return USER
    if os.path.isfile(SYSTEM_ENTRY):
        return SYSTEM
    return None


def install(words=None, env=None):
    """Write the entry for the current user. Returns its path, or raises
    OSError with the reason."""
    text = render(words)
    if text is None:
        raise OSError("this copy does not ship the entry's template")
    path = user_entry(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".new"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)
    return path


def remove(env=None):
    """Remove the current user's entry; a missing one is not an error."""
    try:
        os.remove(user_entry(env))
    except FileNotFoundError:
        pass


def refresh(env=None):
    """Rewrite the user's entry if it no longer starts this copy.

    Only an entry that already exists is touched: this never adds one the
    user did not ask for. And only by an AppImage, the one copy whose path
    changes under it - a source checkout started once must not take over the
    entry that belongs to the installed copy. True when it was rewritten.
    """
    if installation.kind(env) != installation.APPIMAGE:
        return False
    path = user_entry(env)
    try:
        with open(path, encoding="utf-8") as handle:
            current = handle.read()
    except OSError:
        return False
    wanted = render()
    if wanted is None or wanted == current:
        return False
    try:
        install(env=env)
    except OSError as exc:
        log.warning("Could not update %s: %s", path, exc)
        return False
    log.info("Updated the disk utility's right-click entry at %s", path)
    return True
