"""How large a window may open. One place, so tests have one thing to stub.

Every window wants the same thing - a comfortable default size that never
runs off the display - and each had its own copy of it. Collapsing them also
settles a trap: the offscreen platform reports an 800x800 virtual screen, so
any clamp measured in a headless test silently becomes 720 and looks like a
sizing bug in whatever is being measured.
"""

from PyQt6.QtWidgets import QApplication

#: How much of the available desktop a window may fill by default.
FRACTION = 0.9


def screen_limits(widget=None, fraction=FRACTION):
    """The largest (width, height) a window should take, or None.

    None when Qt cannot name a screen, which callers read as "no opinion"
    rather than as a size of zero.
    """
    screen = None
    if widget is not None:
        screen = widget.screen()
    if screen is None:
        screen = QApplication.primaryScreen()
    if screen is None:
        return None
    available = screen.availableGeometry()
    return (int(available.width() * fraction),
            int(available.height() * fraction))


def fit_on_screen(widget, width, height, fraction=FRACTION):
    """A preferred size, shrunk to fit the display it will open on."""
    limits = screen_limits(widget, fraction)
    if limits is None:
        return (width, height)
    return (min(width, limits[0]), min(height, limits[1]))
