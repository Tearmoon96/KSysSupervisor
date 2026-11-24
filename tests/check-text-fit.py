#!/usr/bin/env python3
"""Find every text in the application that does not fit the room it gets.

Runs the whole test suite - which builds every window and dialog in the
states the tests put them in - and after each test shows the windows still
alive and measures each button, checkbox and label in them: cut short in
width, wrapped to fewer lines than it needs, or laid out past the window's
edge.

Measured the way the desktop draws them: the offscreen platform's own font
is narrower than Plasma's, and a text that fits there is cut on screen.
Change FONT and STYLE to check against another desktop.

    python3 tests/check-text-fit.py
"""
import os, sys, unittest, collections
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = ("Noto Sans", 10)                 # Plasma's default
STYLE = "Breeze"
sys.path[:0] = [ROOT, ROOT + "/tests"]
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import (QApplication, QAbstractButton, QLabel, QWidget,
                             QLineEdit, QComboBox)
from PyQt6.QtGui import QFont
app = QApplication.instance() or QApplication([])
# Measured as on the desktop: Plasma's font and style, not offscreen's.
app.setStyle(STYLE)
app.setFont(QFont(*FONT))

seen = set()
found = collections.OrderedDict()

def text_of(w):
    return w.text() if hasattr(w, "text") else ""

def audit(top, where):
    for w in [top] + top.findChildren(QWidget):
        try:
            if not w.isVisibleTo(top) or w.width() <= 1:
                continue
            cls = type(w).__name__
            if cls in ("ElidingLabel",):
                continue
            problem = None
            if isinstance(w, QAbstractButton) and text_of(w):
                # The text itself, not the style's least width for a button.
                text = w.fontMetrics().horizontalAdvance(text_of(w).replace("&", ""))
                room = w.width() - (w.iconSize().width() + 4 if not w.icon().isNull() else 0)
                if type(w).__name__ in ("QCheckBox", "QRadioButton"):
                    room -= 24                   # the box or circle beside it
                else:
                    room -= 8                    # the button's own margins
                if text > room:
                    problem = "cut in width (%d px for %d px of text)" % (room, text)
            elif isinstance(w, QLabel) and text_of(w) and w.pixmap().isNull():
                if w.wordWrap():
                    need = w.heightForWidth(w.width())
                    if need > w.height() + 1:
                        problem = "cut in height (%d < %d)" % (w.height(), need)
                elif w.sizeHint().width() > w.width() + 1:
                    problem = "cut in width (%d < %d)" % (w.width(), w.sizeHint().width())
            if not problem and text_of(w) and isinstance(w, (QAbstractButton, QLabel)):
                # Laid out wider than the window: the window cuts it.
                right = w.mapTo(top, QPoint(w.width(), 0)).x()
                if right > top.width() + 1:
                    problem = "runs %dpx past the window edge" % (right - top.width())
            if problem:
                key = (type(top).__name__, cls, text_of(w)[:70])
                if key not in found:
                    found[key] = (problem, where)
        except RuntimeError:
            continue

original = unittest.TestCase.run
def run(self, result=None):
    out = original(self, result)
    for top in QApplication.topLevelWidgets():
        try:
            if id(top) in seen or top.parent() is not None:
                continue
            name = type(top).__name__
            if name in ("QMenu", "Menu", "KeepOpenMenu", "QToolTip") or not top.layout():
                continue
            seen.add(id(top))
            top.show()
            app.processEvents()
            if hasattr(top, "fit"):
                top.fit(); app.processEvents()
            audit(top, self.id())
            top.hide()
        except RuntimeError:
            continue
    return out
unittest.TestCase.run = run

suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
for (top, cls, text), (problem, where) in found.items():
    print("%-22s %-14s %-48r %s   [%s]" % (top, cls, text, problem, where.rsplit(".", 2)[-2]))
print("windows looked at:", len(seen), "problems:", len(found))
sys.exit(1 if found else 0)
