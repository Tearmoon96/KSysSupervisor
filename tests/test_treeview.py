"""Classic tree tests, offscreen: real items, synthetic readings."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt                               # noqa: E402
from PyQt6.QtWidgets import QApplication                  # noqa: E402

from ksyssupervisor.display import TileBuilder            # noqa: E402
from ksyssupervisor.providers.base import Device, Reading  # noqa: E402
from ksyssupervisor.treeview import SensorTree, row_texts  # noqa: E402


CPU = Device("cpu", "AMD Ryzen 7 9700X", order=10)
RAM = Device("ram", "RAM", order=20)
DISK = Device("disk:sda", "CT1000MX500SSD1 (sda)", order=40)


def sample(temp=61.5, cores=2, disk=True):
    out = [Reading("cpu", "Utilization", "usage_total", "Processor", 42.0,
                   "utilization"),
           Reading("cpu", "Temperatures", "Tctl", "Package", temp, "temp"),
           Reading("ram", "Utilization", "usage", "Memory Used", 12.0,
                   "memory_gb", total=32.0)]
    for i in range(cores):
        out.append(Reading("cpu", "Clocks", "clock_%d" % i, "Core %d" % i,
                           4000.0 + i, "clock"))
    if disk:
        out.append(Reading(DISK.key, "Temperatures", "temp1", "Temp1", 38.0,
                           "temp"))
    return out


def builder():
    return TileBuilder({d.key: d for d in (CPU, RAM, DISK)})


def children(item):
    return [item.child(i) for i in range(item.childCount())]


class TreeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.build = builder()
        self.tree = SensorTree()

    def sync(self, readings, **kwargs):
        self.build.build(readings)
        self.tree.sync(self.build.tree(**kwargs))


class RowTextTest(TreeTestCase):
    def test_an_idle_sensor_shows_its_min_and_max_anyway(self):
        self.build.build(sample())
        temp = dict(self.build.tree()[0].groups)["Temperatures"][0]
        self.assertEqual(row_texts(temp),
                         ("🌡️ Package", "61.5 °C", "61.5 °C", "61.5 °C"))

    def test_a_reading_with_a_ceiling_shows_the_ceiling_as_its_max(self):
        self.build.build(sample())
        used = dict(self.build.tree()[1].groups)["Utilization"][0]
        self.assertEqual(row_texts(used),
                         ("💾 Memory Used", "12.0 / 32.0 GB", "//", "32.0 GB"))


class SyncTest(TreeTestCase):
    def test_devices_groups_and_readings_become_items(self):
        self.sync(sample())
        tops = [self.tree.topLevelItem(i)
                for i in range(self.tree.topLevelItemCount())]
        self.assertEqual([t.text(0) for t in tops],
                         ["AMD Ryzen 7 9700X", "RAM", "CT1000MX500SSD1 (sda)"])
        self.assertEqual([t.data(0, Qt.ItemDataRole.UserRole) for t in tops],
                         ["cpu", "ram", "disk:sda"])
        groups = [g.text(0) for g in children(tops[0])]
        self.assertEqual(groups, ["Temperatures", "Utilization", "Clocks"])
        clocks = children(tops[0])[2]
        self.assertEqual([c.text(0) for c in children(clocks)],
                         ["⏱️ Core 0", "⏱️ Core 1"])

    def test_a_second_sync_updates_the_same_items(self):
        self.sync(sample(temp=50.0))
        before = dict(self.tree._rows)
        self.sync(sample(temp=70.0))
        self.assertEqual(set(before), set(self.tree._rows))
        row = self.tree._rows["cpu/Tctl"]
        self.assertIs(row, before["cpu/Tctl"])
        self.assertEqual(row.text(1), "70.0 °C")
        self.assertEqual(row.text(2), "50.0 °C")
        self.assertEqual(row.text(3), "70.0 °C")

    def test_a_reading_that_is_gone_takes_its_row_with_it(self):
        self.sync(sample(cores=2))
        self.sync(sample(cores=1), group_visible={"Clocks": False})
        cpu = self.tree.topLevelItem(0)
        self.assertEqual([g.text(0) for g in children(cpu)],
                         ["Temperatures", "Utilization"])

    def test_a_rename_lands_on_the_category(self):
        self.sync(sample())
        self.sync(sample(), names={"cpu": "Desk"})
        self.assertEqual(self.tree.topLevelItem(0).text(0), "Desk")

    def test_new_nodes_open_unless_told_to_collapse(self):
        self.sync(sample())
        cpu = self.tree.topLevelItem(0)
        self.assertTrue(cpu.isExpanded())
        self.assertTrue(children(cpu)[0].isExpanded())
        self.tree.set_all_expanded(False)
        self.assertFalse(cpu.isExpanded())


class OrderTest(TreeTestCase):
    def test_order_reports_the_keys_as_shown(self):
        self.sync(sample())
        self.assertEqual(self.tree.order(), ["cpu", "ram", "disk:sda"])

    def test_apply_order_moves_the_named_and_keeps_the_rest(self):
        self.sync(sample())
        self.tree.apply_order(["disk:sda", "nope", "cpu"])
        self.assertEqual(self.tree.order(), ["disk:sda", "cpu", "ram"])

    def test_reordering_keeps_the_expansion(self):
        self.sync(sample())
        self.tree.topLevelItem(0).setExpanded(False)
        self.tree.apply_order(["ram"])
        self.assertEqual(self.tree.order(), ["ram", "cpu", "disk:sda"])
        self.assertFalse(self.tree.topLevelItem(1).isExpanded())
        self.assertTrue(self.tree.topLevelItem(0).isExpanded())


class MenuTest(TreeTestCase):
    def test_the_menu_asks_the_window_by_device_key(self):
        self.sync(sample())
        asked = []
        self.tree.rename_requested.connect(asked.append)
        self.tree.clear_bounds_requested.connect(lambda k: asked.append("clear:" + k))
        menu = self.tree.menu_for(self.tree.topLevelItem(2))
        actions = [a for a in menu.actions() if not a.isSeparator()]
        self.assertEqual([a.text() for a in actions],
                         ["Rename...", "Reset name", "Clear min/max here"])
        actions[0].trigger()
        actions[2].trigger()
        self.assertEqual(asked, ["disk:sda", "clear:disk:sda"])


class WidthTest(TreeTestCase):
    def test_the_wanted_width_grows_with_the_content(self):
        from PyQt6.QtWidgets import QMainWindow
        window = QMainWindow()
        window.setCentralWidget(self.tree)
        window.resize(300, 400)
        window.show()
        empty = self.tree.wanted_width(window)
        self.sync(sample(cores=8))
        self.app.processEvents()
        full = self.tree.wanted_width(window)
        self.assertGreater(full, empty)
