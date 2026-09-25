"""Icons the desktop theme does not provide.

Two gaps this closes. The application's own icon is only in the icon theme once
something has installed it there, which is not true of a checkout or of an
AppImage, so it is loaded from the copy shipped beside the package instead. And
Breeze - like most themes - has no fan icon at all, so the one next to Fan
Control is drawn here rather than left blank.

Kept apart from the windows so the drawing is not tangled up with the widgets,
and out of __init__.py, which deliberately imports nothing but the standard
library so a missing PyQt6 can still be reported.
"""

import os

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap

from . import log

#: Rendered at this size and scaled down by Qt where a menu wants it smaller;
#: drawing straight into a 16px pixmap loses the blades to aliasing.
DRAW_SIZE = 64

BLADES = 3

_CACHE = {}


def _package_root():
    """The directory holding Icons/, whether installed, checked out or bundled."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_icon():
    """The application icon, for the window frames and the task bar.

    The theme is asked first so a proper installation keeps whatever the user's
    icon theme resolves, and the shipped SVG is the fallback for every way of
    running this that never touches the icon theme.
    """
    themed = QIcon.fromTheme("ksyssupervisor")
    if not themed.isNull():
        return themed

    path = os.path.join(_package_root(), "Icons", "iconblue.svg")
    if os.path.isfile(path):
        icon = QIcon(path)
        if not icon.isNull():
            return icon
        # An SVG that will not load usually means the Qt svg image plugin is
        # missing, which is worth saying: everything else still works.
        log.warning("The application icon at %s could not be loaded", path)
    else:
        log.warning("No application icon found at %s", path)
    return QIcon()


def fan_icon(colour):
    """A fan, drawn because no common icon theme ships one.

    Breeze, Adwaita and the rest have no fan icon, so QIcon.fromTheme returns
    nothing and the menu entry ends up as the only one without a picture. The
    theme is still asked first, in case a desktop does have one.

    `colour` should come from the palette, so the icon suits a light and a dark
    theme alike rather than being a fixed grey that disappears into one of them.
    """
    themed = QIcon.fromTheme("sensors-fan")
    if not themed.isNull():
        return themed

    key = colour.name()
    if key in _CACHE:
        return _CACHE[key]

    icon = QIcon(_draw_fan(colour))
    _CACHE[key] = icon
    return icon


def _draw_fan(colour):
    """Blades swept round a hub: the shape reads as a fan even at 16 pixels."""
    pixmap = QPixmap(DRAW_SIZE, DRAW_SIZE)
    pixmap.fill(QColor(0, 0, 0, 0))

    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))

        centre = DRAW_SIZE / 2.0
        painter.translate(centre, centre)

        # Each blade is a filled paddle, elongated along the radius and tilted
        # about its own centre. Both parts matter at 16 pixels: a thin drawn
        # curve collapses into an asterisk, a round one reads as a flower, and
        # without the tilt there is no sense of rotation. Three blades rather
        # than four - the odd number keeps it from looking like a cross.
        for index in range(BLADES):
            painter.save()
            painter.rotate(index * (360.0 / BLADES))
            painter.translate(0.0, -15.0)
            painter.rotate(25.0)
            painter.drawEllipse(QRectF(-8.0, -16.0, 16.0, 32.0))
            painter.restore()

        # The hub sits on top of the blades, with a hole punched through it, so
        # the centre stays legible instead of filling in when the icon is
        # scaled down to a menu.
        painter.drawEllipse(QRectF(-8.0, -8.0, 16.0, 16.0))
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Clear)
        painter.drawEllipse(QRectF(-3.5, -3.5, 7.0, 7.0))
    finally:
        painter.end()

    return pixmap
