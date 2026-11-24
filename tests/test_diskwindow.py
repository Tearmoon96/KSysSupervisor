"""The disk utility window and its dialogs, offscreen.

The window is given a fixed list of drives, canned health and a client that
records what it is asked to run, with threads switched off, so each test sees
the result of an action as soon as the action returns. No disk is read and
nothing is ever written.
"""

import gc
import json
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QSettings                          # noqa: E402
from PyQt6.QtGui import QPalette                            # noqa: E402
from PyQt6.QtWidgets import QApplication                    # noqa: E402

from test_disks import (FIXTURE, installer_stick, parse,    # noqa: E402
                        split_stick, stick)

from ksyssupervisor import (diskactivity, diskctl, diskfs,  # noqa: E402
                            diskhealth, diskops, disks)
from ksyssupervisor import diskwindow as window_mod         # noqa: E402
from ksyssupervisor.diskdialogs import (ConfirmDialog,      # noqa: E402
                                        FormatDialog, RenameDialog)
from ksyssupervisor.diskwidgets import PartitionMap         # noqa: E402

GIB = 1024 ** 3
MIB = 1024 ** 2


def machine():
    with open(FIXTURE) as handle:
        return disks.parse(json.load(handle), fstab=[]) + parse(stick())


def healthy(**fields):
    health = diskhealth.Health(status=diskhealth.GOOD, kind="ata",
                               temperature=30, power_on_hours=1000,
                               updated=1)
    for key, value in fields.items():
        setattr(health, key, value)
    return health


class FakeClient:
    """Stands in for the helper's client: remembers what it was asked."""

    def __init__(self):
        self.requests = []
        self.events = []             # played back to on_event for each run
        self.result = {"device": "/dev/sde1", "label": "NEW", "uuid": "x"}
        self.error = None
        self.cancelled = 0
        self.stopped = None

    def run(self, request, on_event=None):
        self.requests.append(request)
        for event in self.events:
            # A callable is something that happens while the job runs.
            if callable(event):
                event()
            else:
                on_event(event)
        if self.error is not None:
            raise self.error
        return dict(self.result)

    def cancel(self):
        self.cancelled += 1

    def stop(self, patience=30):
        self.stopped = patience


class DiskWindowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        original = window_mod.fit_on_screen
        window_mod.fit_on_screen = lambda w, a, b, fraction=0.9: (a, b)
        self.addCleanup(setattr, window_mod, "fit_on_screen", original)
        self.settings = QSettings("KSysSupervisorTest", "DiskWindow")
        self.settings.clear()
        # Windows and dialogs made here have no parent, so they live until
        # Python collects them - and a collection that happens to run in
        # the middle of some later test's paint event takes widgets away
        # under Qt's feet. Collected here instead, between tests.
        self.addCleanup(gc.collect)
        self.machine = machine()
        self.health = {d.name: healthy() for d in self.machine if d.has_media}
        self.client = FakeClient()
        self.messages = []
        self.mounted = []
        self.timer = True                # the weekly TRIM, as the system has it
        # What the drives under a job report, by name: diskactivity.Counters.
        self.counters = {}
        self.watched = []                # the drives each job watched
        self.kernel = []                 # what the kernel says meanwhile
        # Which drives a job is on is read from this machine's /sys; the
        # tests say it instead.
        self.job_drives = ["sde"]
        patch = mock.patch.object(window_mod.diskactivity, "devices_of",
                                  lambda request: list(self.job_drives))
        patch.start()
        self.addCleanup(patch.stop)
        self.answers = []                # how the dialogs to come are answered
        # Mount options and leftover files are read from the real system;
        # the fixture's mount points must not find this machine's.
        for name, value in (("MOUNTINFO", "/nonexistent/mountinfo"),
                            ("leftovers", lambda volume, listdir=None: [])):
            patch = mock.patch.object(window_mod.diskmaintain, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # Nor this machine's record of an interrupted partition move.
        from ksyssupervisor import diskparts
        patch = mock.patch.object(diskparts, "MOVES_DIR",
                                  "/nonexistent/moves")
        patch.start()
        self.addCleanup(patch.stop)
        # Nothing here may reach the real disk service or the real helper.
        for name, value in (
                ("mount", lambda device: self.mounted.append(device)
                 or "/run/media/user/X"),
                ("unmount", lambda device: self.mounted.append("-" + device)),
                ("power_off", lambda device: self.mounted.append("off"))):
            patch = mock.patch.object(window_mod.udisks, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(window_mod.diskctl, "availability",
                                  return_value=(diskctl.READY, None))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(window_mod.disks, "identity",
                                  side_effect=lambda name: {"name": name})
        patch.start()
        self.addCleanup(patch.stop)
        # The right-click entry lives in the user's own data directory; the
        # tests keep their own idea of whether it is there.
        self.entry = {"where": None, "calls": []}
        for name, value in (
                ("installed", lambda env=None: self.entry["where"]),
                ("install", lambda words=None, env=None: (
                    self.entry["calls"].append("install"),
                    self.entry.update(where=window_mod.diskentry.USER))),
                ("remove", lambda env=None: (
                    self.entry["calls"].append("remove"),
                    self.entry.update(where=None)))):
            patch = mock.patch.object(window_mod.diskentry, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def window(self, select=None):
        window = window_mod.DiskUtilityWindow(
            settings=self.settings, select=select,
            inventory=lambda: list(self.machine),
            health=lambda targets, refresh=False: dict(self.health),
            client=self.client, background=False,
            timer=lambda: self.timer)
        window._new_monitor = lambda names: (
            self.watched.append(list(names)),
            diskactivity.Monitor(names, read=self.counters.get))[1]
        window._read_kernel = lambda name, since: list(self.kernel)
        window.notify = lambda title, text, warning=True: \
            self.messages.append((title, text))
        # Dialogs are answered here: each one shown is kept, and accepted
        # or not as the test has said.
        self.dialogs = []
        window.show_dialog = lambda dialog: (
            self.dialogs.append(dialog),
            self.answers.pop(0) if self.answers else False)[1]
        self.addCleanup(window.shutdown)
        return window

    def tooltip(self, button):
        return button.toolTip()

    def assert_dialog_fits(self, dialog):
        """Every wrapped text in a dialog on screen has the height its
        lines need, and every advice card more than a sliver.

        To be called after something changed while the dialog was shown:
        cards added then used to be left out of the dialog's height."""
        from PyQt6.QtWidgets import QLabel
        from ksyssupervisor.diskdialogs import AdviceCard
        self.app.processEvents()
        for label in dialog.findChildren(QLabel):
            if label.isVisible() and label.wordWrap() and label.text():
                self.assertGreaterEqual(
                    label.height(), label.heightForWidth(label.width()),
                    "cut short: %r" % label.text()[:60])
        for card in dialog.findChildren(AdviceCard):
            if card.isVisible():
                self.assertGreaterEqual(
                    card.height(), card.heightForWidth(card.width()),
                    "cut short: %r" % card.advice.title)


class ListTest(DiskWindowCase):
    def test_built_in_drives_come_first_and_empty_readers_last(self):
        window = self.window()
        self.assertEqual(list(window.drive_cards),
                         ["sda", "sdb", "sdc", "nvme0n1", "nvme1n1", "sde",
                          "sdd"])

    def test_the_first_drive_is_selected_whole(self):
        window = self.window()
        self.assertEqual(window.selected_disk, "sda")
        self.assertIsNone(window.selected_volume)
        self.assertTrue(window.drive_cards["sda"].is_selected())
        self.assertTrue(window.header.is_selected())
        self.assertEqual(list(window.volume_rows), ["sda1", "sda2"])

    def test_two_drives_of_one_model_are_told_apart(self):
        window = self.window()
        first = window.drive_cards["sdb"].subtitle.text()
        second = window.drive_cards["sdc"].subtitle.text()
        self.assertNotEqual(first, second)
        self.assertIn("sdb", first)

    def test_the_system_drive_says_so(self):
        window = self.window()
        self.assertFalse(window.drive_cards["nvme0n1"].system.isHidden())
        self.assertTrue(window.drive_cards["sdb"].system.isHidden())

    def test_a_drive_or_volume_can_be_asked_for_by_path(self):
        window = self.window(select="/dev/sdb1")
        self.assertEqual((window.selected_disk, window.selected_volume),
                         ("sdb", "sdb1"))
        self.assertTrue(window.volume_rows["sdb1"].is_selected())
        self.assertFalse(window.header.is_selected())
        window.select_device("nvme1n1")
        self.assertEqual((window.selected_disk, window.selected_volume),
                         ("nvme1n1", None))

    def test_health_reaches_the_cards(self):
        self.health["sdb"] = healthy(life_left=94)
        self.health["sda"] = healthy(status=diskhealth.CAUTION,
                                     reasons=["8 sectors went bad."])
        window = self.window()
        self.assertEqual(window.drive_cards["sdb"].health.text(),
                         "Good · 94% life")
        self.assertEqual(window.drive_cards["sda"].health.text(), "Caution")
        self.assertIn("8 sectors", window.drive_cards["sda"].health.toolTip())
        self.assertEqual(window.drive_cards["sdb"].temperature.text(), "30 °C")

    def test_a_drive_that_disappears_takes_the_selection_with_it(self):
        window = self.window(select="sde1")
        self.assertEqual(window.selected_disk, "sde")
        self.machine = [d for d in self.machine if d.name != "sde"]
        window.refresh()
        self.assertNotIn("sde", window.drive_cards)
        self.assertEqual(window.selected_disk, "sda")
        self.assertIsNone(window.selected_volume)

    def test_an_unchanged_list_rebuilds_nothing(self):
        window = self.window()
        card, row = window.drive_cards["sda"], window.volume_rows["sda1"]
        window.refresh()
        self.assertIs(window.drive_cards["sda"], card)
        self.assertIs(window.volume_rows["sda1"], row)

    def test_a_failed_read_keeps_what_was_shown(self):
        window = self.window()

        def broken():
            raise disks.InventoryError("lsblk failed: boom")
        window._read_inventory = broken
        window.refresh()
        self.assertIn("boom", window.status.text())
        self.assertEqual(len(window.drive_cards), 7)


class VolumeTest(DiskWindowCase):
    def test_a_mounted_volume_shows_how_full_it_is(self):
        window = self.window(select="sdb")
        row = window.volume_rows["sdb1"]
        self.assertIn("used", row.usage.text())
        self.assertGreater(row.bar.fraction(), 0.5)
        self.assertIn("/mnt/giochi", row.where.text())

    def test_an_unmounted_volume_is_not_guessed_at(self):
        window = self.window(select="sda")
        row = window.volume_rows["sda2"]
        self.assertEqual(row.usage.text(), "usage unknown")
        self.assertIsNone(row.bar.fraction())
        self.assertIn("not mounted", row.where.text())

    def test_the_map_has_a_segment_per_partition_in_order(self):
        window = self.window(select="nvme1n1")
        self.assertEqual([s[0] for s in window.map.segments()],
                         ["nvme1n1p1", "nvme1n1p2", "nvme1n1p3", "nvme1n1p4"])

    def test_a_small_partition_is_still_wide_enough_to_click(self):
        """A 16 MB partition on a 500 GB drive would be a third of a pixel."""
        window = self.window(select="nvme1n1")
        placed = window.map.layout_segments(800)
        self.assertTrue(all(rect.width() >= PartitionMap.MIN_SEGMENT
                            for rect, _segment in placed))
        self.assertAlmostEqual(placed[-1][0].right(), 800, delta=1)
        widest = max(placed, key=lambda p: p[0].width())
        self.assertEqual(widest[1][0], "nvme1n1p3")

    def test_clicking_the_map_selects_that_volume(self):
        window = self.window(select="sda")
        window.map.picked.emit("sda2")
        self.assertEqual(window.selected_volume, "sda2")
        self.assertTrue(window.volume_rows["sda2"].is_selected())
        self.assertFalse(window.header.is_selected())
        window.header.clicked.emit("")
        self.assertIsNone(window.selected_volume)

    def test_an_empty_reader_says_what_it_is_waiting_for(self):
        window = self.window(select="sdd")
        self.assertIn("no media", window.empty_note.text())
        self.assertTrue(window.map.isHidden())


class ActionTest(DiskWindowCase):
    """Greyed out, never hidden, and saying why."""

    def test_nothing_destructive_is_offered_on_the_system_drive(self):
        window = self.window(select="nvme0n1")
        self.assertFalse(window.format_button.isEnabled())
        self.assertIn("running system", window.format_button.toolTip())
        self.assertFalse(window.eject_button.isEnabled())
        for name in ("nvme0n1p1", "nvme0n1p2", "nvme0n1p3"):
            window.select_volume(name)
            for button in (window.format_button, window.mount_button,
                           window.rename_button):
                self.assertFalse(button.isEnabled(), (name, button.text()))
                self.assertIn("running system", button.toolTip())
            self.assertEqual(window.mount_button.text(), "Unmount")
        for button in (window.format_button, window.mount_button,
                       window.rename_button, window.eject_button):
            self.assertFalse(button.isHidden())

    def test_a_data_volume_offers_its_actions(self):
        window = self.window(select="sdb1")
        self.assertTrue(window.format_button.isEnabled())
        self.assertTrue(window.rename_button.isEnabled())
        self.assertEqual(window.mount_button.text(), "Unmount")
        self.assertTrue(window.mount_button.isEnabled())
        self.assertFalse(window.eject_button.isEnabled())
        self.assertIn("internal drive", window.eject_button.toolTip())

    def test_the_whole_drive_asks_for_a_volume_where_one_is_needed(self):
        window = self.window(select="sdb")
        self.assertFalse(window.mount_button.isEnabled())
        self.assertIn("Select a volume", window.mount_button.toolTip())
        self.assertTrue(window.format_button.isEnabled())

    def test_mounting_goes_through_the_desktops_own_service(self):
        window = self.window(select="sda2")
        self.assertEqual(window.mount_button.text(), "Mount")
        window.toggle_mount()
        self.assertEqual(self.mounted, ["/dev/sda2"])
        self.assertIn("/run/media/user/X", window.status.text())

    def test_unmounting_a_system_volume_is_not_even_attempted(self):
        window = self.window(select="nvme0n1p3")
        window.toggle_mount()
        self.assertEqual(self.mounted, [])

    def test_ejecting_unmounts_then_powers_off(self):
        window = self.window(select="sde")
        window.eject_selected()
        self.assertEqual(self.mounted, ["-/dev/sde1", "off"])
        self.assertIn("can be removed", window.status.text())

    def test_a_failed_mount_is_reported_in_its_own_words(self):
        def refuse(device):
            raise window_mod.udisks.UDisksError("Not authorized")
        with mock.patch.object(window_mod.udisks, "mount", refuse):
            window = self.window(select="sda2")
            window.toggle_mount()
        self.assertEqual(self.messages[-1][1], "Not authorized")


class ToolMenusTest(DiskWindowCase):
    """Maintain, Copy, Partitions and Recover Files sit in the selected
    drive's banner at the top."""

    def menus(self, window):
        return [window.maintain_button, window.copy_button,
                window.partitions_button, window.recover_button]

    def shown(self, window, width):
        window.resize(width, 760)
        window.show()
        self.app.processEvents()

    def test_they_are_in_the_drives_banner(self):
        window = self.window(select="sde")
        for button in self.menus(window):
            self.assertIs(button.parentWidget(), window.header.tools)
        self.assertEqual([b.text() for b in self.menus(window)],
                         ["Maintain", "Copy", "Partitions", "Recover Files"])

    def test_beside_the_title_when_wide_on_their_own_line_when_not(self):
        window = self.window(select="sde")
        self.shown(window, 1400)
        self.assertTrue(window.header.tools_beside())
        top = window.header.title.mapTo(window.header, window.header.title
                                        .rect().topLeft()).y()
        menu = window.maintain_button.mapTo(window.header, window.maintain_button
                                            .rect().topLeft()).y()
        self.assertLess(abs(menu - top), window.maintain_button.height())
        self.shown(window, 820)
        self.assertFalse(window.header.tools_beside())
        below = window.maintain_button.mapTo(
            window.header, window.maintain_button.rect().topLeft()).y()
        self.assertGreater(below, window.header.subtitle.mapTo(
            window.header, window.header.subtitle.rect().topLeft()).y())
        self.shown(window, 1400)
        self.assertTrue(window.header.tools_beside())

    def test_they_do_not_make_the_banner_wider_than_it_must_be(self):
        window = self.window(select="sde")
        tools = window.header.tools.sizeHint().width()
        self.assertLess(window.header.minimumSizeHint().width(), tools + 120)

    def test_each_says_what_it_holds_and_greys_out_without_media(self):
        window = self.window(select="sde")
        for button in self.menus(window):
            self.assertIn("what is selected", button.toolTip())
        window.select_disk("sdd")                 # a reader with no card
        for button in self.menus(window)[:3]:
            self.assertFalse(button.isEnabled())


class RightClickEntryTest(DiskWindowCase):
    def test_the_switch_adds_and_removes_the_users_entry(self):
        window = self.window()
        self.assertFalse(window.entry_action.isChecked())
        window.entry_action.setChecked(True)
        self.assertEqual(self.entry["calls"], ["install"])
        self.assertTrue(window.entry_action.isChecked())
        window.entry_action.setChecked(False)
        self.assertEqual(self.entry["calls"], ["install", "remove"])

    def test_an_entry_the_installer_put_there_is_not_ours_to_remove(self):
        self.entry["where"] = window_mod.diskentry.SYSTEM
        window = self.window()
        self.assertTrue(window.entry_action.isChecked())
        self.assertFalse(window.entry_action.isEnabled())
        self.assertIn("all users", window.entry_action.toolTip())

    def test_a_failure_is_reported_and_the_switch_tells_the_truth(self):
        def refuse(words=None, env=None):
            raise OSError("Read-only file system")
        with mock.patch.object(window_mod.diskentry, "install", refuse):
            window = self.window()
            window.entry_action.setChecked(True)
        self.assertIn("Read-only file system", self.messages[-1][1])
        self.assertFalse(window.entry_action.isChecked())


class EntryFileTest(unittest.TestCase):
    """The entry itself, written into a temporary data directory."""

    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp()
        self.env = {"HOME": self.home, "XDG_DATA_HOME":
                    os.path.join(self.home, "data")}
        import shutil
        self.addCleanup(shutil.rmtree, self.home, True)

    def test_it_starts_this_copy_on_the_device_that_was_clicked(self):
        from ksyssupervisor import diskentry
        path = diskentry.install(["/opt/apps/KSys.AppImage"], env=self.env)
        self.assertEqual(path, os.path.join(
            self.home, "data", "solid", "actions",
            "ksyssupervisor_diskutility.desktop"))
        with open(path) as handle:
            text = handle.read()
        self.assertIn("Exec=/opt/apps/KSys.AppImage --disk-utility=%d", text)
        self.assertIn("Name=KSysSupervisor Disk Utility", text)
        self.assertIn("X-KDE-Solid-Predicate=", text)
        self.assertNotIn("@EXEC@", text)
        self.assertEqual(diskentry.installed(self.env), diskentry.USER)
        diskentry.remove(self.env)
        diskentry.remove(self.env)          # twice: gone is gone
        self.assertFalse(os.path.exists(path))

    def test_a_path_with_spaces_is_quoted_as_one_word(self):
        from ksyssupervisor import diskentry
        text = diskentry.render(["/home/me/My Apps/K$ys 100%.AppImage"])
        self.assertIn('Exec="/home/me/My Apps/K\\$ys 100%%.AppImage" '
                      '--disk-utility=%d', text)

    def test_only_an_appimage_follows_its_own_renaming(self):
        """A checkout started once must not take the installed copy's entry
        for itself."""
        from ksyssupervisor import diskentry, installation
        diskentry.install(["/old/KSys-1.0.AppImage"], env=self.env)
        self.assertFalse(diskentry.refresh(self.env))

        appimage = os.path.join(self.home, "KSys-1.1.AppImage")
        open(appimage, "w").close()
        env = dict(self.env, APPIMAGE=appimage, APPDIR=installation.APP_DIR)
        with mock.patch.object(diskentry, "command",
                               return_value=[appimage]):
            self.assertTrue(diskentry.refresh(env))
            self.assertFalse(diskentry.refresh(env))     # already current
        with open(diskentry.user_entry(self.env)) as handle:
            self.assertIn("Exec=%s " % appimage, handle.read())

    def test_refreshing_never_adds_an_entry_nobody_asked_for(self):
        from ksyssupervisor import diskentry, installation
        appimage = os.path.join(self.home, "K.AppImage")
        open(appimage, "w").close()
        env = dict(self.env, APPIMAGE=appimage, APPDIR=installation.APP_DIR)
        self.assertFalse(diskentry.refresh(env))
        self.assertIsNone(diskentry.installed(env)
                          if not os.path.isfile(diskentry.SYSTEM_ENTRY)
                          else None)


class JobTest(DiskWindowCase):
    def plan(self, window, name="sde"):
        disk = [d for d in self.machine if d.name == name][0]
        return diskops.plan_format(disk, None, "exfat", "NEW", identity={})

    def test_a_plan_goes_to_the_helper_as_it_was_confirmed(self):
        window = self.window(select="sde")
        plan = self.plan(window)
        window.run_plan(plan)
        self.assertEqual(self.client.requests, [plan.request])
        self.assertIsNone(window._job)
        self.assertTrue(window.job_panel.isHidden())
        self.assertIn("Formatted /dev/sde1 as 'NEW'", window.status.text())
        # Ready to use afterwards: a stick is plugged in to be used.
        self.assertEqual(self.mounted, ["/dev/sde1"])

    def test_the_desktop_mounting_it_first_is_not_a_failure(self):
        def already(device):
            raise window_mod.udisks.UDisksError(
                "Device /dev/sde1 is already mounted at `/run/media/u/NEW'.")
        window = self.window(select="sde")
        with mock.patch.object(window_mod.udisks, "mount", already):
            window.run_plan(self.plan(window))
        self.assertEqual(window.status.text(),
                         "Formatted /dev/sde1 as 'NEW'.")

    def test_a_volume_that_could_not_be_mounted_again_is_mentioned(self):
        def refuse(device):
            raise window_mod.udisks.UDisksError("Not authorized")
        window = self.window(select="sde")
        with mock.patch.object(window_mod.udisks, "mount", refuse), \
                mock.patch.object(window_mod.log, "info"):
            window.run_plan(self.plan(window))
        self.assertIn("It is not mounted: Not authorized",
                      window.status.text())
        self.assertEqual(self.messages, [])

    def test_steps_and_progress_are_shown_as_they_arrive(self):
        window = self.window(select="sde")
        seen = []
        self.client.events = [
            {"event": "step", "index": 1, "total": 3, "key": "erase",
             "text": "Overwriting /dev/sde with zeros", "cancellable": True},
            {"event": "progress", "done": 2 * GIB, "total": 8 * GIB},
        ]
        original = window._on_job_event

        def watch(event):
            original(event)
            seen.append((window.job_step.text(), window.job_bar.value(),
                         window.stop_button.isEnabled(),
                         window.format_button.isEnabled()))
        window._on_job_event = watch
        window.run_plan(self.plan(window))
        self.assertEqual(seen[0][0],
                         "Step 1 of 3: Overwriting /dev/sde with zeros")
        self.assertTrue(seen[0][2])
        self.assertEqual(seen[1][1], 250)
        # Nothing else can be started while it runs.
        self.assertFalse(seen[0][3])
        self.assertTrue(window.format_button.isEnabled())

    def test_a_step_that_must_finish_cannot_be_stopped(self):
        window = self.window(select="sde")
        states = []
        self.client.events = [{"event": "step", "index": 2, "total": 3,
                               "key": "table", "text": "Creating a table",
                               "cancellable": False}]
        original = window._on_job_event
        window._on_job_event = lambda event: (
            original(event), states.append(window.stop_button.isEnabled()))
        window.run_plan(self.plan(window))
        self.assertEqual(states, [False])

    def test_a_refusal_is_shown_in_the_helpers_words(self):
        window = self.window(select="sde")
        self.client.error = diskctl.HelperError(
            "/dev/sde is not the device that was selected any more")
        with mock.patch.object(window_mod.log, "warning"):
            window.run_plan(self.plan(window))
        self.assertEqual(self.messages[-1][0], "Format did not complete")
        self.assertIn("not the device", self.messages[-1][1])
        self.assertEqual(self.mounted, [])
        self.assertTrue(window.format_button.isEnabled())

    def test_a_stop_is_not_an_error(self):
        window = self.window(select="sde")
        self.client.error = diskctl.Cancelled("Stopped before anything on "
                                              "the device was changed.")
        window.run_plan(self.plan(window))
        self.assertEqual(self.messages, [])
        self.assertIn("Stopped before", window.status.text())

    def test_a_warning_with_the_result_is_passed_on(self):
        window = self.window(select="sde")
        self.client.result["warning"] = "Windows may not show this volume."
        window.run_plan(self.plan(window))
        self.assertIn("Windows may not show", self.messages[-1][1])

    def test_no_helper_and_no_way_to_install_one_is_explained(self):
        window = self.window(select="sde")
        with mock.patch.object(window_mod.diskctl, "availability",
                               return_value=(diskctl.NO_PKEXEC, "needs "
                                             "pkexec")), \
                mock.patch.object(window_mod.diskctl, "can_install_helper",
                                  return_value=False):
            window.run_plan(self.plan(window))
        self.assertEqual(self.client.requests, [])
        self.assertIn("needs pkexec", self.messages[-1][1])

    def test_closing_with_nothing_running_lets_the_helper_go(self):
        window = self.window()
        self.assertTrue(window.confirm_shutdown())
        window.shutdown()
        self.assertTrue(window.is_finished())
        self.assertEqual(self.client.stopped, 30)

    def test_the_identity_sent_is_the_one_read_with_the_list(self):
        window = self.window(select="sde1")
        self.assertEqual(window.identities["sde1"], {"name": "sde1"})
        self.assertEqual(window.identities["sde"], {"name": "sde"})


class JobDetailsTest(DiskWindowCase):
    """The details of a running job, and the warning when its drive stops
    answering."""

    def plan(self, op="format"):
        disk = [d for d in self.machine if d.name == "sde"][0]
        plan = diskops.plan_format(disk, None, "ext4", "NEW", identity={})
        plan.request["op"] = op
        return plan

    def at(self, when, completed, waiting, written=0):
        self.counters["sde"] = diskactivity.Counters(
            when, 0, written, completed, waiting)

    def during(self, window, *moments):
        """Events for the fake helper: a step, then each moment's counters
        sampled and what the window showed recorded."""
        seen = []

        def moment(when, completed, waiting, written=0):
            def happen():
                self.at(when, completed, waiting, written)
                window._sample_activity()
                seen.append({
                    "cards": [c.advice.title for c in window._stall_cards],
                    "text": [c.details.text() for c in window._stall_cards],
                    "activity": window.job_activity.text(),
                    "log": window.job_log.toPlainText()})
            return happen
        self.client.events = [
            {"event": "step", "index": 3, "total": 4, "key": "mkfs",
             "text": "Creating the filesystem", "cancellable": False},
        ] + [moment(*m) for m in moments]
        return seen

    def test_what_the_tool_says_is_shown_under_the_bar_and_logged(self):
        window = self.window(select="sde")
        seen = []
        self.client.events = [
            {"event": "step", "index": 3, "total": 4, "key": "mkfs",
             "text": "Creating the filesystem", "cancellable": False},
            {"event": "output", "text": "Writing inode tables:  12/954"},
            {"event": "progress", "done": 12, "total": 954,
             "unit": "items"},
            lambda: seen.append((window.job_rate.text(),
                                 window.job_bar.maximum(),
                                 window.job_bar.value(),
                                 window.job_log.toPlainText())),
        ]
        window.run_plan(self.plan())
        rate, maximum, value, logged = seen[0]
        # A count of things, not bytes: the tool's own words stay.
        self.assertEqual(rate, "Writing inode tables:  12/954")
        self.assertEqual((maximum, value), (1000, 12 * 1000 // 954))
        self.assertIn("Step 3 of 4: Creating the filesystem", logged)
        self.assertIn("Writing inode tables:  12/954", logged)

    def test_byte_progress_keeps_the_rate_line(self):
        window = self.window(select="sde")
        seen = []
        self.client.events = [
            {"event": "step", "index": 1, "total": 1, "key": "copy",
             "text": "Copying", "cancellable": True},
            {"event": "progress", "done": GIB, "total": 8 * GIB},
            {"event": "output", "text": "rescued: 1 GB"},
            lambda: seen.append(window.job_rate.text()),
        ]
        window.run_plan(self.plan("clone"))
        self.assertIn("of 8.6 GB", seen[0])

    def test_a_drive_that_stops_answering_is_warned_about(self):
        window = self.window(select="sde")
        self.at(0.0, 100, 0)
        seen = self.during(window,
                           (1.0, 120, 4, 10 * MIB),   # working
                           (30.0, 120, 4),            # quiet, not yet
                           (95.0, 120, 4),            # over a minute
                           (96.0, 121, 2))            # and it answers
        window.run_plan(self.plan())
        self.assertEqual(self.watched, [["sde"]])
        self.assertEqual(seen[0]["cards"], [])
        self.assertIn("working", seen[0]["activity"])
        self.assertEqual(seen[1]["cards"], [])
        self.assertIn("no answer for 29 s", seen[1]["activity"])
        self.assertEqual(seen[2]["cards"], [diskactivity.STALL_TITLE])
        self.assertIn("for 1 min 34 s", seen[2]["text"][0])
        # A format: the drive's contents are going anyway.
        self.assertIn("loses nothing", seen[2]["text"][0])
        self.assertIn("sde: no answer for 1 min 34 s", seen[2]["log"])
        self.assertEqual(seen[3]["cards"], [])
        self.assertIn("sde paused for 1 min 34 s, then carried on",
                      seen[3]["log"])
        # Gone with the job.
        self.assertTrue(window.job_panel.isHidden())
        self.assertIsNone(window._monitor)

    def test_a_drive_being_changed_is_not_to_be_unplugged(self):
        window = self.window(select="sde")
        self.at(0.0, 100, 3)
        seen = self.during(window, (70.0, 100, 3))
        window.run_plan(self.plan("part_resize"))
        self.assertIn("Do not unplug", seen[0]["text"][0])

    def test_what_the_kernel_said_is_explained_with_the_warning(self):
        window = self.window(select="sde")
        self.kernel = ["usb 2-1: reset high-speed USB device number 4 "
                       "using xhci_hcd"]
        self.at(0.0, 100, 3)
        seen = self.during(window, (70.0, 100, 3))
        window.run_plan(self.plan())
        self.assertEqual(seen[0]["cards"], [
            diskactivity.STALL_TITLE,
            "The USB connection does not come up properly"])
        self.assertIn("reset high-speed USB device", seen[0]["log"])

    def test_a_drive_whose_counters_cannot_be_read_is_said_so(self):
        window = self.window(select="sde")
        seen = []
        self.client.events = [lambda: (
            window._sample_activity(),
            seen.append((window.job_activity.text(),
                         list(window._stall_cards))))]
        window.run_plan(self.plan())
        self.assertIn("cannot be read", seen[0][0])
        self.assertEqual(seen[0][1], [])

    def test_the_details_stay_open_or_closed_as_left(self):
        window = self.window(select="sde")
        self.assertTrue(window.job_details.isHidden())
        window.job_details_toggle.click()
        self.assertFalse(window.job_details.isHidden())
        again = self.window(select="sde")
        self.assertFalse(again.job_details.isHidden())


class DetailsTest(DiskWindowCase):
    def test_drive_facts(self):
        sda = [d for d in self.machine if d.name == "sda"][0]
        facts = dict(window_mod.drive_facts(sda, healthy(rotation_rate=5400)))
        self.assertEqual(facts["Type"], "HDD, 5,400 rpm")
        self.assertEqual(facts["Connection"], "SATA")
        self.assertEqual(facts["Partition table"], "GPT")
        self.assertEqual(facts["Sector size"], "512 bytes, 4096 physical")
        self.assertIn("8,001,563,222,016 bytes", facts["Capacity"])
        self.assertNotIn("TRIM", facts)

        sde = [d for d in self.machine if d.name == "sde"][0]
        self.assertEqual(dict(window_mod.drive_facts(sde))["TRIM"],
                         "Not passed on by this device")

    def test_health_facts_show_only_what_the_drive_reports(self):
        sdb = [d for d in self.machine if d.name == "sdb"][0]
        facts = dict(window_mod.health_facts(sdb, healthy(
            life_left=94, power_on_hours=17136, written=7_593_367_552_000)))
        self.assertEqual(facts["Life left"], "94%")
        self.assertEqual(facts["Powered on"],
                         "17,136 hours (about 2.0 years)")
        self.assertEqual(facts["Data written"], "7.6 TB")
        self.assertNotIn("Spare area", facts)
        self.assertNotIn("Reallocated sectors", facts)

    def test_an_ssd_that_reports_no_wear_says_so(self):
        sdb = [d for d in self.machine if d.name == "sdb"][0]
        facts = dict(window_mod.health_facts(sdb, healthy()))
        self.assertEqual(facts["Life left"], "Not reported by this drive")
        sda = [d for d in self.machine if d.name == "sda"][0]
        self.assertNotIn("Life left",
                         dict(window_mod.health_facts(sda, healthy())))

    def test_no_data_explains_itself(self):
        unknown = diskhealth.unknown(diskhealth.NO_SMART)
        advice = window_mod.health_advice(unknown)
        self.assertEqual(advice[0].title, "No health data")
        self.assertIn("memory sticks", advice[0].text)

    def test_the_reasons_for_a_verdict_are_shown_with_it(self):
        self.health["sda"] = healthy(status=diskhealth.CAUTION,
                                     reasons=["8 sectors went bad."],
                                     notes=["12 interface CRC errors."])
        window = self.window(select="sda")
        shown = [card.advice.title for card in window._advice_cards]
        self.assertEqual(shown, ["Health: Caution", "Worth knowing"])

    def test_the_attribute_table_follows_the_selected_drive(self):
        self.health["sdb"] = healthy(attributes=[diskhealth.Attribute(
            5, "Reallocated sector count", 9, 9, 10, 4, 3)])
        window = self.window(select="sdb")
        self.assertEqual(window.attributes.topLevelItemCount(), 1)
        self.assertFalse(window._attributes_header.isHidden())
        window.select_disk("sda")
        self.assertEqual(window.attributes.topLevelItemCount(), 0)
        self.assertTrue(window._attributes_header.isHidden())


class FormatDialogTest(DiskWindowCase):
    def everything(self, *names):
        return "/usr/sbin/" + names[0]

    def dialog(self, name="sde", volume=None, tools=None):
        disk = [d for d in self.machine if d.name == name][0]
        return FormatDialog(disk, disk.volume(volume) if volume else None,
                            tools=tools or self.everything, identity={})

    def choose(self, dialog, key):
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData(key))

    def test_advice_added_on_screen_gets_its_room(self):
        dialog = self.dialog()
        dialog.show()
        self.addCleanup(dialog.close)
        self.app.processEvents()
        self.choose(dialog, "ext4")
        self.assertTrue(dialog.advice_cards)
        self.assert_dialog_fits(dialog)

    def test_a_stick_starts_on_the_recommended_filesystem(self):
        dialog = self.dialog()
        self.assertEqual(dialog.filesystem.currentData(), "vfat")
        self.assertIn("recommended", dialog.filesystem.currentText())
        self.assertFalse(dialog.advanced_header.isHidden())

    def test_ntfs_on_a_small_stick_warns_before_anything_is_confirmed(self):
        dialog = self.dialog()
        self.choose(dialog, "ntfs")
        titles = [card.advice.title for card in dialog.advice_cards]
        self.assertIn("NTFS is a poor choice for a small removable drive",
                      titles)
        self.assertTrue(dialog.start.isEnabled())

    def test_a_name_too_long_blocks_the_button_and_says_why(self):
        dialog = self.dialog()
        dialog.label.setText("far too long a name")
        self.assertFalse(dialog.start.isEnabled())
        self.assertIn("Too long", dialog.label_note.text())
        dialog.label.setText("photos")
        self.assertTrue(dialog.start.isEnabled())
        self.assertIn("stored as PHOTOS", dialog.label_note.text())
        self.assertEqual(dialog.build_plan().request["label"], "PHOTOS")

    def test_a_missing_tool_leaves_the_entry_there_but_greyed(self):
        dialog = self.dialog(
            tools=lambda *names: None if "mkfs.fat" in names else "/x")
        row = dialog.filesystem.findData("vfat")
        item = dialog.filesystem.model().item(row)
        self.assertFalse(item.isEnabled())
        self.assertIn("dosfstools", item.toolTip())
        # And the dialog opens on something that can be made instead.
        self.assertNotEqual(dialog.filesystem.currentData(), "vfat")

    def test_ownership_is_only_asked_where_it_exists(self):
        dialog = self.dialog()
        self.assertTrue(dialog.owner.isHidden())
        self.choose(dialog, "ext4")
        self.assertFalse(dialog.owner.isHidden())
        self.assertTrue(dialog.build_plan().request["owner"])
        dialog.owner.setChecked(False)
        self.assertFalse(dialog.build_plan().request["owner"])

    def test_a_partition_has_no_table_to_choose(self):
        dialog = self.dialog("sdb", "sdb1")
        self.assertTrue(dialog.advanced_header.isHidden())
        self.assertEqual(dialog.label.text(), "Fedora-G")
        plan = dialog.build_plan()
        self.assertIsNone(plan.request["table"])
        self.assertEqual(plan.request["device"], "/dev/sdb1")

    def test_full_says_what_it_will_do_to_this_kind_of_drive(self):
        dialog = self.dialog("sdb", "sdb1")
        dialog.quick.setChecked(False)
        self.assertIn("TRIM", dialog.quick_note.text())
        self.assertEqual(dialog.build_plan().request["erase"], "discard")
        dialog = self.dialog("sda", "sda2")
        dialog.quick.setChecked(False)
        self.assertIn("zeros", dialog.quick_note.text())
        self.assertIn("hours", dialog.quick_note.text())

    def test_mbr_is_not_offered_where_it_cannot_reach(self):
        dialog = self.dialog("sda")
        self.assertEqual(dialog.table.currentData(), "gpt")
        self.assertFalse(dialog.table.model().item(1).isEnabled())

    def test_an_xfs_name_takes_no_space(self):
        dialog = self.dialog("sdb", "sdb1")
        self.choose(dialog, "xfs")
        dialog.label.setText("my disk")
        self.assertFalse(dialog.start.isEnabled())
        self.assertIn("space", dialog.label_note.text())

    def test_the_dialog_is_as_tall_as_its_text_at_its_own_width(self):
        """Sized for the width it would like, the last card was cut off."""
        dialog = self.dialog()
        self.choose(dialog, "ntfs")
        dialog.show()
        self.app.processEvents()
        wanted = dialog.layout().totalHeightForWidth(dialog.width())
        self.assertEqual(dialog.height(), wanted)
        card = dialog.advice_cards[0]
        self.assertGreaterEqual(card.height(),
                                card.layout().totalHeightForWidth(card.width()))


class FormatClusterTest(DiskWindowCase):
    """The cluster size: every size offered, the usual one first."""

    def everything(self, *names):
        return "/usr/sbin/" + names[0]

    def dialog(self, name="sde", volume=None):
        disk = [d for d in self.machine if d.name == name][0]
        return FormatDialog(disk, disk.volume(volume) if volume else None,
                            tools=self.everything, identity={})

    def choose(self, dialog, key):
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData(key))

    def pick(self, dialog, cluster):
        dialog.cluster.combo.setCurrentIndex(
            dialog.cluster.combo.findData(cluster))

    def test_the_recommended_size_is_chosen_and_marked(self):
        dialog = self.dialog()                   # an 8 GB stick, FAT32
        combo = dialog.cluster.combo
        self.assertTrue(combo.isEnabled())
        self.assertEqual(combo.currentData(), 4096)
        self.assertIn("(recommended)", combo.currentText())
        self.assertEqual(sum("recommended" in combo.itemText(i)
                             for i in range(combo.count())), 1)
        plan = dialog.build_plan()
        self.assertEqual(plan.request["cluster"], 4096)
        self.assertIn("Create the FAT32 filesystem with 4 KiB clusters",
                      plan.steps)

    def test_another_size_can_be_chosen_and_its_cost_is_said(self):
        dialog = self.dialog()
        self.choose(dialog, "exfat")
        self.assertEqual(dialog.cluster.value(), 32768)
        note = dialog.cluster.note
        self.assertEqual(note.foregroundRole(),
                         QPalette.ColorRole.PlaceholderText)
        self.pick(dialog, MIB)
        self.assertIn("even a 1\u00a0KB file uses 1\u00a0MiB", note.text())
        self.assertIn("waste a lot of space", note.text())
        self.assertEqual(note.foregroundRole(), QPalette.ColorRole.WindowText)
        self.assertEqual(dialog.build_plan().request["cluster"], MIB)

    def test_a_choice_lasts_until_the_filesystem_changes(self):
        dialog = self.dialog()
        self.choose(dialog, "exfat")
        self.pick(dialog, 4096)
        dialog.label.setText("photos")
        self.assertEqual(dialog.cluster.value(), 4096)
        self.choose(dialog, "ntfs")
        self.assertEqual(dialog.cluster.value(), 4096)
        self.pick(dialog, 65536)
        self.choose(dialog, "exfat")
        self.assertEqual(dialog.cluster.value(), 32768)

    def test_a_linux_filesystem_shows_its_size_greyed(self):
        dialog = self.dialog()
        self.choose(dialog, "ext4")
        combo = dialog.cluster.combo
        self.assertFalse(combo.isEnabled())
        self.assertFalse(dialog.cluster.row.isHidden())
        self.assertEqual(combo.currentText(), "4 KiB")
        self.assertIn("memory pages", combo.toolTip())
        self.assertIsNone(dialog.build_plan().request["cluster"])
        self.assertNotIn("clusters", " ".join(dialog.build_plan().steps))

    def test_a_longer_note_gets_its_room(self):
        dialog = self.dialog()
        self.choose(dialog, "ntfs")
        dialog.show()
        self.addCleanup(dialog.close)
        self.app.processEvents()
        self.pick(dialog, 2 * MIB)
        self.assertIn("1709", dialog.cluster.note.text())
        self.assert_dialog_fits(dialog)


class VolumeClusterTest(DiskWindowCase):
    def test_a_volume_row_shows_its_cluster_size_where_known(self):
        import dataclasses
        from ksyssupervisor.diskwidgets import VolumeRow
        volume = parse(stick(fstype="exfat"))[0].volumes[0]
        row = VolumeRow(dataclasses.replace(volume, cluster=MIB))
        self.assertTrue(row.usage.text().endswith("· 1 MiB clusters"))
        self.assertIn("uses 1 MiB", row.usage.toolTip())
        row.set_volume(dataclasses.replace(volume, cluster=None))
        self.assertNotIn("clusters", row.usage.text())
        self.assertEqual(row.usage.toolTip(), "")


class FormatScopeTest(DiskWindowCase):
    """One partition, or the whole drive put back together."""

    def everything(self, *names):
        return "/usr/sbin/" + names[0]

    def dialog(self, entry, volume, identities=None):
        disk = parse(entry)[0]
        return FormatDialog(disk, disk.volume(volume) if volume else None,
                            tools=self.everything,
                            identities=identities or {})

    def test_a_partition_offers_itself_and_the_whole_drive(self):
        dialog = self.dialog(split_stick(), "sdg1")
        self.assertTrue(dialog.scope_part.isChecked())
        self.assertTrue(dialog.scope_whole.isEnabled())
        self.assertIn("sdg1", dialog.scope_part.text())
        self.assertIn("restore to one volume", dialog.scope_whole.text())
        self.assertIn("all 2 partitions", dialog.whole_note.text())
        self.assertTrue(dialog.advanced_header.isHidden())
        self.assertIn("This drive is split up",
                      [card.advice.title for card in dialog.advice_cards])

    def test_the_drive_itself_has_nothing_to_choose(self):
        dialog = self.dialog(split_stick(), None)
        self.assertIsNone(dialog.scope_whole)
        self.assertTrue(dialog.whole)
        self.assertEqual(dialog.build_plan().request["device"], "/dev/sdg")

    def test_choosing_the_whole_drive_changes_what_is_planned(self):
        identities = {"sdg": {"name": "sdg"}, "sdg1": {"name": "sdg1"}}
        dialog = self.dialog(split_stick(), "sdg1", identities)
        plan = dialog.build_plan()
        self.assertEqual(plan.request["device"], "/dev/sdg1")
        self.assertEqual(plan.request["identity"], {"name": "sdg1"})
        self.assertIsNone(plan.request["table"])
        self.assertEqual(len(plan.loses), 1)

        dialog.scope_whole.setChecked(True)
        self.assertFalse(dialog.advanced_header.isHidden())
        plan = dialog.build_plan()
        self.assertEqual(plan.request["device"], "/dev/sdg")
        # The identity of the drive, as it was read with the list.
        self.assertEqual(plan.request["identity"], {"name": "sdg"})
        self.assertIn(plan.request["table"], ("gpt", "dos"))
        self.assertEqual(len(plan.loses), 2)
        self.assertIn("table", plan.step_keys)
        # Nothing tells the user to choose what is already chosen.
        self.assertNotIn("This drive is split up",
                         [card.advice.title for card in dialog.advice_cards])

        dialog.scope_part.setChecked(True)
        self.assertEqual(dialog.build_plan().request["device"], "/dev/sdg1")
        self.assertTrue(dialog.advanced_header.isHidden())

    def test_the_filesystem_chosen_survives_the_switch(self):
        dialog = self.dialog(split_stick(), "sdg1")
        dialog.filesystem.setCurrentIndex(dialog.filesystem.findData("ext4"))
        dialog.scope_whole.setChecked(True)
        self.assertEqual(dialog.filesystem.currentData(), "ext4")
        self.assertEqual(dialog.build_plan().request["fstype"], "ext4")

    def test_a_partition_too_small_for_a_filesystem_gets_it_on_the_drive(self):
        """The boot partition of an installer stick is a megabyte or two:
        what fits there and what fits on the drive are different lists."""
        entry = split_stick()
        entry["children"][1]["size"] = 40 * 1024 * 1024
        dialog = self.dialog(entry, "sdg2")
        row = dialog.filesystem.findData("btrfs")
        self.assertFalse(dialog.filesystem.model().item(row).isEnabled())
        dialog.scope_whole.setChecked(True)
        row = dialog.filesystem.findData("btrfs")
        self.assertTrue(dialog.filesystem.model().item(row).isEnabled())

    def test_an_installer_stick_starts_on_the_whole_drive(self):
        dialog = self.dialog(installer_stick(), "sdf1")
        self.assertTrue(dialog.scope_whole.isChecked())
        plan = dialog.build_plan()
        self.assertEqual(plan.request["device"], "/dev/sdf")
        # The image's name is not offered as the name of the drive...
        self.assertEqual(dialog.label.text(), "")
        self.assertTrue(dialog.start.isEnabled())
        dialog.scope_part.setChecked(True)
        # ...only as the name of the partition that carries it.
        self.assertEqual(dialog.label.text(), "Fedora-WS-Live")
        dialog.scope_whole.setChecked(True)
        self.assertEqual(dialog.label.text(), "")
        dialog.label.setText("MINE")
        dialog.scope_part.setChecked(True)
        self.assertEqual(dialog.label.text(), "MINE")
        dialog.scope_part.setChecked(True)
        self.assertIn("This drive was written as an installer image",
                      [card.advice.title for card in dialog.advice_cards])

    def test_the_whole_system_drive_is_greyed_and_says_why(self):
        disk = disks.Disk("sdz", "/dev/sdz", 500 * GIB, table="gpt", volumes=(
            disks.Volume("sdz1", "/dev/sdz1", "sdz", "part", 100 * GIB,
                         fstype="ext4", mountpoints=("/",)),
            disks.Volume("sdz2", "/dev/sdz2", "sdz", "part", 399 * GIB,
                         fstype="ext4", label="Data")))
        dialog = FormatDialog(disk, disk.volumes[1], tools=self.everything)
        self.assertFalse(dialog.scope_whole.isEnabled())
        self.assertTrue(dialog.scope_part.isChecked())
        self.assertIn("running system", dialog.whole_note.text())
        self.assertFalse(dialog.whole)

    def test_the_window_hands_over_what_it_read_with_the_list(self):
        self.machine = self.machine + parse(split_stick())
        window = self.window()
        window.select_device("sdg1")
        seen = {}

        class Dialog:
            def __init__(self, disk, volume, parent, identities=None):
                seen.update(disk=disk.name, volume=volume.name,
                            identities=identities)

            def exec(self):
                return 0

        with mock.patch.object(window_mod, "FormatDialog", Dialog):
            window.format_selected()
        self.assertEqual((seen["disk"], seen["volume"]), ("sdg", "sdg1"))
        self.assertIs(seen["identities"], window.identities)


class ConfirmDialogTest(DiskWindowCase):
    def plan(self, name, volume=None):
        disk = [d for d in self.machine if d.name == name][0]
        return diskops.plan_format(disk, disk.volume(volume) if volume
                                   else None, "ext4", "x", identity={})

    def test_enter_never_erases_anything(self):
        dialog = ConfirmDialog(self.plan("sde"))
        self.assertTrue(dialog.cancel.isDefault())
        self.assertFalse(dialog.go.isDefault())
        self.assertFalse(dialog.go.autoDefault())

    def test_a_removable_drive_needs_no_tick(self):
        dialog = ConfirmDialog(self.plan("sde"))
        self.assertTrue(dialog.understood.isHidden())
        self.assertTrue(dialog.go.isEnabled())

    def test_a_built_in_drive_wants_a_tick_first(self):
        dialog = ConfirmDialog(self.plan("sdb", "sdb1"))
        self.assertFalse(dialog.understood.isHidden())
        self.assertFalse(dialog.go.isEnabled())
        dialog.understood.setChecked(True)
        self.assertTrue(dialog.go.isEnabled())


class RenameDialogTest(DiskWindowCase):
    def dialog(self, name, volume):
        disk = [d for d in self.machine if d.name == name][0]
        return RenameDialog(disk, disk.volume(volume), identity={})

    def test_the_same_name_is_not_a_rename(self):
        dialog = self.dialog("sdb", "sdb1")
        self.assertFalse(dialog.rename.isEnabled())
        dialog.label.setText("Steam")
        self.assertTrue(dialog.rename.isEnabled())
        plan = dialog.build_plan()
        self.assertEqual(plan.request, {"op": "label", "device": "/dev/sdb1",
                                        "identity": {}, "label": "Steam"})

    def test_the_limit_is_the_filesystems_own(self):
        dialog = self.dialog("sdb", "sdb1")
        dialog.label.setText("seventeen letters")
        self.assertFalse(dialog.rename.isEnabled())
        self.assertIn("16", dialog.note.text())
        self.assertEqual(diskfs.FILESYSTEMS["ext4"].label_max, 16)


class MainWindowTest(unittest.TestCase):
    """The ways in from the main window."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_tools_menu_and_the_drive_menus_open_it(self):
        from ksyssupervisor import window as main_mod
        original = main_mod.fit_on_screen
        main_mod.fit_on_screen = lambda w, a, b, fraction=0.9: (a, b)
        self.addCleanup(setattr, main_mod, "fit_on_screen", original)
        settings = QSettings("KSysSupervisorTest", "DiskEntry")
        settings.clear()
        main = main_mod.KSysSupervisor(settings=settings)
        self.addCleanup(main._stop_worker)
        self.assertEqual(main.disk_action.text(), "Disk Utility...")

        opened = []

        class Stub:
            def __init__(self, settings, select=None):
                opened.append(("new", select))

            def is_finished(self):
                return False

            def select_device(self, device):
                opened.append(("select", device))

            def confirm_shutdown(self, parent=None):
                return True

            def shutdown(self):
                opened.append(("shutdown", None))

            show = raise_ = activateWindow = close = lambda self: None

        with mock.patch.object(window_mod, "DiskUtilityWindow", Stub):
            main.disk_action.trigger()
            main.tree.disk_utility_requested.emit("sdb")
            main.board.disk_utility_requested.emit("nvme0n1")
            self.assertTrue(main.close_tools())
        self.assertEqual(opened, [("new", None), ("select", "sdb"),
                                  ("select", "nvme0n1"), ("shutdown", None)])
        self.assertIsNone(main._disk_window)

    def test_the_standalone_flag_takes_an_optional_device(self):
        from ksyssupervisor import app
        args, _rest = app.parse_args(["--disk-utility=/dev/sdb1"])
        self.assertEqual(args.disk_utility, "/dev/sdb1")
        args, _rest = app.parse_args(["--disk-utility"])
        self.assertEqual(args.disk_utility, "")
        args, _rest = app.parse_args([])
        self.assertIsNone(args.disk_utility)
        for flag in ("--install-fan-helper", "--install-helpers"):
            args, _rest = app.parse_args([flag])
            self.assertTrue(args.install_helpers)


if __name__ == "__main__":
    unittest.main()
