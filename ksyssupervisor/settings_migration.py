"""Carrying settings across the rename from KSysMonitor.

Before its first public release the application was called KSysMonitor, and
QSettings keys its storage by application name - so on the first run under the
new name the preferences look empty: category order and visibility, renamed
categories, renamed fans, window geometry. None of it is important enough to warn about and
all of it is annoying to redo, so it is copied over once.

Deliberately a copy rather than a move: if a user goes back to the old version
for any reason, their settings are still there.
"""

from . import log

OLD_ORGANISATION = "KSysMonitor"
NEW_ORGANISATION = "KSysSupervisor"
APPLICATION = "Settings"

#: Written once the copy has happened, so it never runs twice - importantly,
#: not even after the user has deliberately cleared something.
MARKER = "migrated_from_ksysmonitor"


def migrate(new=None, old=None):
    """Copy the old settings across if this is the first run under the new name.

    Returns the number of keys copied; 0 when there was nothing to do.
    """
    from PyQt6.QtCore import QSettings

    new = new if new is not None else QSettings(NEW_ORGANISATION, APPLICATION)
    if new.value(MARKER, False, type=bool):
        return 0

    old = old if old is not None else QSettings(OLD_ORGANISATION, APPLICATION)
    keys = old.allKeys()
    if not keys:
        # Nothing to carry over - a fresh install, not a rename. Still marked,
        # so a later KSysMonitor install cannot suddenly overwrite settings
        # this one has since accumulated.
        new.setValue(MARKER, True)
        return 0

    copied = 0
    for key in keys:
        if key == MARKER or new.contains(key):
            # Anything already set under the new name wins: it was chosen more
            # recently than whatever the old install had.
            continue
        new.setValue(key, old.value(key))
        copied += 1

    new.setValue(MARKER, True)
    new.sync()
    log.info("Carried %d setting(s) over from KSysMonitor", copied)
    return copied
