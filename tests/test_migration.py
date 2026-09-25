"""Carrying settings across the rename.

QSettings keys its storage by application name, so the rename would silently
empty every preference the user had. These pin that it does not, and that the
copy cannot run twice or overwrite newer choices.
"""

import os
import unittest


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PyQt6.QtCore import QSettings          # noqa: F401
        except ImportError:                             # pragma: no cover
            raise unittest.SkipTest("PyQt6 is not available")

    def pair(self, name):
        """A fresh old/new settings pair, isolated from the real ones."""
        from PyQt6.QtCore import QSettings

        old = QSettings("KSysMonitorTest-%s-old" % name, "Settings")
        new = QSettings("KSysMonitorTest-%s-new" % name, "Settings")
        old.clear(); new.clear()
        self.addCleanup(old.clear)
        self.addCleanup(new.clear)
        return old, new

    def test_settings_are_carried_over(self):
        from ksyssupervisor.settings_migration import migrate

        old, new = self.pair("carry")
        old.setValue("fan_name_nct6799:3", "Front intake")
        old.setValue("show_cat_Fans", False)
        old.sync()

        self.assertEqual(migrate(new, old), 2)
        self.assertEqual(new.value("fan_name_nct6799:3"), "Front intake")

    def test_it_only_runs_once(self):
        """Otherwise a later edit would be undone every startup."""
        from ksyssupervisor.settings_migration import migrate

        old, new = self.pair("once")
        old.setValue("show_cat_Fans", False)
        old.sync()

        self.assertEqual(migrate(new, old), 1)
        new.setValue("show_cat_Fans", True)          # user changes their mind
        self.assertEqual(migrate(new, old), 0)
        self.assertTrue(new.value("show_cat_Fans", type=bool))

    def test_newer_choices_are_not_overwritten(self):
        from ksyssupervisor.settings_migration import migrate

        old, new = self.pair("newer")
        old.setValue("fan_name_x:1", "old name")
        new.setValue("fan_name_x:1", "new name")
        old.sync(); new.sync()

        migrate(new, old)
        self.assertEqual(new.value("fan_name_x:1"), "new name")

    def test_a_fresh_install_is_marked_without_copying(self):
        """So a KSysMonitor installed later cannot overwrite settings this one
        has accumulated since."""
        from ksyssupervisor.settings_migration import migrate

        old, new = self.pair("fresh")
        self.assertEqual(migrate(new, old), 0)
        self.assertTrue(new.value("migrated_from_ksysmonitor", type=bool))


if __name__ == "__main__":
    unittest.main()
