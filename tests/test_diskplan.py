"""Prepared changes on the recovery stick: the picture they make, how they
are found again when they run, and the window holding them back until
Apply Changes."""

import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, MIB                              # noqa: E402
from test_diskwindow import DiskWindowCase                 # noqa: E402
from test_diskcopy import machine                          # noqa: E402

from ksyssupervisor import (diskcopy, diskctl, diskmaintain,  # noqa: E402
                            diskops, diskparts, diskplan, disks)


def identities(view):
    """What the window holds: every device's identity, a diskseq each."""
    found = {}
    for number, disk in enumerate(view, 1):
        found[disk.name] = {"name": disk.name, "diskseq": number}
        for volume in disk.volumes:
            found[volume.name] = {"name": volume.name, "diskseq": number}
    return found


class PictureTest(unittest.TestCase):
    """What the drives look like with changes prepared. sdb: an MBR drive
    with one ext4 partition over all of it; nvme0n1: the system drive."""

    def setUp(self):
        self.real = list(machine().values())
        self.ids = identities(self.real)
        self.queue = diskplan.Queue()

    def disk(self, name, view=None):
        return {d.name: d for d in (view or self.view())}[name]

    def view(self):
        return self.queue.view(self.real)

    def prepare(self, plan, **extra):
        return self.queue.add(plan, self.view(), self.ids, extra=extra)

    def test_writing_is_held_back_reading_is_not(self):
        for request, held in (
                ({"op": "format"}, True), ({"op": "part_move"}, True),
                ({"op": "clone"}, True), ({"op": "recover_files"}, True),
                ({"op": "check", "repair": True}, True),
                ({"op": "check", "repair": False}, False),
                ({"op": "part_limits"}, False),
                ({"op": "partition_search"}, False),
                ({"op": "raid_build"}, False)):
            self.assertEqual(diskplan.is_prepared(request), held, request)

    def test_delete_then_create_in_the_room_it_left(self):
        sdb = self.disk("sdb")
        self.prepare(diskparts.plan_delete(sdb, sdb.volume("sdb1"), {}))
        sdb = self.disk("sdb")
        self.assertEqual(sdb.volumes, ())
        self.assertEqual(sdb.pending, "changed")
        self.assertEqual(len(sdb.free), 1)
        self.prepare(diskparts.plan_create(sdb, MIB, 100 * GIB, "ntfs",
                                           "Data", identity={}))
        sdb = self.disk("sdb")
        new = sdb.volume("sdb1")
        self.assertEqual((new.start, new.size, new.fstype, new.label,
                          new.pending), (MIB, 100 * GIB, "ntfs", "Data",
                                         "new"))
        self.assertTrue(sdb.free)
        # The drive as it really is has not changed.
        self.assertEqual(self.disk("sdb", self.real).volume("sdb1").fstype,
                         "ext4")

    def test_a_whole_drive_format_is_one_partition_over_it(self):
        sdb = self.disk("sdb")
        self.prepare(diskops.plan_format(sdb, None, "exfat", "Big",
                                         table="gpt", identity={}))
        sdb = self.disk("sdb")
        self.assertEqual(sdb.table, "gpt")
        (only,) = sdb.volumes
        self.assertEqual((only.start, only.fstype, only.label, only.pending),
                         (MIB, "exfat", "Big", "formatted"))
        self.assertLessEqual(only.start + only.size,
                             sdb.size - 33 * sdb.logical_sector)

    def test_resize_and_move_change_the_picture(self):
        sdb = self.disk("sdb")
        volume = sdb.volume("sdb1")
        self.prepare(diskparts.plan_resize(sdb, volume, 500 * GIB, {}))
        volume = self.disk("sdb").volume("sdb1")
        self.assertEqual((volume.size, volume.pending), (500 * GIB,
                                                         "resized"))
        self.prepare(diskparts.plan_move(self.disk("sdb"), volume,
                                         10 * GIB, {}))
        volume = self.disk("sdb").volume("sdb1")
        self.assertEqual((volume.start, volume.size), (10 * GIB, 500 * GIB))
        self.assertEqual(volume.pending, "resized")

    def test_a_clone_copies_the_layout_and_grows_the_last(self):
        source, target = self.disk("nvme0n1"), self.disk("sdb")
        source = dataclasses.replace(source, volumes=tuple(
            dataclasses.replace(v, mountpoints=()) for v in source.volumes))
        self.real = [source if d.name == "nvme0n1" else d for d in self.real]
        plan = diskcopy.plan_clone(source, None, target, None, grow=True,
                                   identities={}, tools=lambda *n: "/x")
        self.prepare(plan)
        copied = self.disk("sdb")
        self.assertEqual(copied.table, "gpt")
        self.assertEqual([v.name for v in copied.volumes],
                         ["sdb1", "sdb2", "sdb3"])
        self.assertTrue(all(v.pending == "copied" for v in copied.volumes))
        grow = diskcopy.grow_candidate(source, target)
        self.assertEqual(copied.volume("sdb3").size, grow.size)
        # The source is only read.
        self.assertEqual(self.disk("nvme0n1").pending, "")

    def test_an_image_leaves_the_drive_unknown_until_applied(self):
        sdb = self.disk("sdb")
        plan = diskcopy.plan_image_write(sdb, None, "/tmp/x.img",
                                         {"kind": "disk", "size": GIB},
                                         identity={})
        self.prepare(plan)
        self.assertEqual(self.disk("sdb").pending, "image")
        with self.assertRaises(diskplan.PlanError) as caught:
            self.prepare(diskparts.plan_table_new(self.disk("sdb"), "gpt",
                                                  {}))
        self.assertIn("Apply the changes first", str(caught.exception))

    def test_taking_one_back_takes_what_was_built_on_it(self):
        sdb = self.disk("sdb")
        delete = self.prepare(diskparts.plan_delete(sdb, sdb.volume("sdb1"),
                                                    {}))
        sde = self.disk("sde")
        other = self.prepare(diskmaintain.plan_trim(sde, sde.volume("sde1"),
                                                    {}))
        create = self.prepare(diskparts.plan_create(
            self.disk("sdb"), MIB, GIB, "ext4", identity={}))
        self.assertEqual(self.queue.dependents(delete), [create])
        self.assertEqual(self.queue.remove(delete), [delete, create])
        self.assertEqual(self.queue.changes, [other])
        self.assertEqual(self.disk("sdb").volume("sdb1").fstype, "ext4")

    def test_reading_what_is_prepared_is_refused_limits_are_not(self):
        sdb = self.disk("sdb")
        self.prepare(diskparts.plan_resize(sdb, sdb.volume("sdb1"),
                                           500 * GIB, {}))
        view = self.view()
        check = diskmaintain.plan_check(self.disk("sdb", view),
                                        self.disk("sdb", view).volume("sdb1"),
                                        repair=False, identity={})
        self.assertIn("Apply them first", diskplan.reading_problem(
            check.request, view, self.queue))
        limits = {"op": "part_limits", "device": "/dev/sdb1"}
        self.assertEqual(diskplan.reading_problem(limits, view, self.queue),
                         "")
        sde = self.disk("sde")
        other = diskmaintain.plan_check(sde, sde.volume("sde1"),
                                        repair=False, identity={})
        self.assertEqual(diskplan.reading_problem(other.request, view,
                                                  self.queue), "")

    def test_the_room_of_a_partition_is_the_pictures(self):
        sdb = self.disk("sdb")
        self.prepare(diskparts.plan_resize(sdb, sdb.volume("sdb1"),
                                           400 * GIB, {}))
        sdb = self.disk("sdb")
        volume = sdb.volume("sdb1")
        limits = diskplan.limits(sdb, volume, {"smallest": 3 * GIB,
                                               "grow": "", "shrink": ""})
        self.assertEqual(limits["size"], 400 * GIB)
        self.assertEqual(limits["smallest"], 3 * GIB)
        self.assertGreater(limits["largest"], 900 * GIB)
        new = diskplan.limits(sdb, dataclasses.replace(volume,
                                                       pending="new",
                                                       fstype="xfs"))
        self.assertIn("never smaller", new["shrink"])

    def test_the_summary_names_the_drives_written_to(self):
        source, target = self.disk("sdc"), self.disk("sdb")
        self.prepare(diskcopy.plan_clone(source, None, target, None,
                                         identities={}))
        summary = diskplan.summary(self.queue.changes)
        self.assertEqual(summary.target, "to sdb")
        self.assertTrue(summary.internal)
        self.assertEqual(summary.confirm, "Apply Changes")


class FindingAgainTest(unittest.TestCase):
    """Just before it runs, each change's devices are named as they are."""

    def setUp(self):
        self.real = list(machine().values())
        self.ids = identities(self.real)
        self.queue = diskplan.Queue()

    def identity(self, name):
        return {"name": name, "diskseq": self.ids.get(name, self.ids.get(
            name[:3], {})).get("diskseq")}

    def test_a_partition_made_earlier_is_found_where_it_was_put(self):
        sdb = {d.name: d for d in self.real}["sdb"]
        self.queue.add(diskparts.plan_table_new(sdb, "gpt", {}), self.real,
                       self.ids)
        view = self.queue.view(self.real)
        self.queue.add(diskparts.plan_create(
            {d.name: d for d in view}["sdb"], 2 * MIB, GIB, "ext4",
            identity={}), view, self.ids)
        view = self.queue.view(self.real)
        new = {d.name: d for d in view}["sdb"].volumes[0]
        rename = diskops.plan_rename({d.name: d for d in view}["sdb"], new,
                                     "Fresh", identity={})
        change = self.queue.add(rename, view, self.ids)
        # On the drive now: the table and the partition the first two made,
        # under a name the kernel chose.
        made = disks.Volume("sdb7", "/dev/sdb7", "sdb", "part", GIB,
                            start=2 * MIB, number=1, fstype="ext4")
        now = [dataclasses.replace(d, volumes=(made,), table="gpt")
               if d.name == "sdb" else d for d in self.real]
        request = self.queue.request_for(change, now, self.identity)
        self.assertEqual(request["device"], "/dev/sdb7")
        self.assertEqual(request["identity"]["name"], "sdb7")
        self.assertEqual(request["label"], "Fresh")
        # Not there: nothing is guessed.
        empty = [dataclasses.replace(d, volumes=()) if d.name == "sdb"
                 else d for d in self.real]
        with self.assertRaises(diskplan.PlanError):
            self.queue.request_for(change, empty, self.identity)

    def test_a_drive_plugged_in_again_is_not_taken_for_the_same(self):
        sdb = {d.name: d for d in self.real}["sdb"]
        change = self.queue.add(diskparts.plan_table_new(sdb, "gpt", {}),
                                self.real, self.ids)
        with self.assertRaises(diskplan.PlanError) as caught:
            self.queue.request_for(change, self.real,
                                   lambda name: {"diskseq": 99})
        self.assertIn("unplugged", str(caught.exception))


class QueueWindowTest(DiskWindowCase):
    """The Disk Utility as the stick runs it."""

    def window(self, select=None):
        window = super().window(select)
        window.queue = diskplan.Queue()
        window.refresh()
        window._apply_inventory(list(self.machine), {
            d.name: {"name": d.name} for d in self.machine}, None)
        return window

    def delete_plan(self, window):
        sdb = [d for d in window.disks if d.name == "sdb"][0]
        return diskparts.plan_delete(sdb, sdb.volume("sdb1"), {})

    def test_a_writing_operation_is_prepared_not_run(self):
        window = self.window()
        window.run_plan(self.delete_plan(window))
        self.assertEqual(self.client.requests, [])
        self.assertEqual(len(window.queue), 1)
        self.assertIn("Prepared", window.status.text())
        sdb = [d for d in window.disks if d.name == "sdb"][0]
        self.assertEqual(sdb.volumes, ())
        real = [d for d in window.real_disks if d.name == "sdb"][0]
        self.assertTrue(real.volumes)

    def test_apply_runs_the_list_in_order_and_empties_it(self):
        window = self.window()
        window.run_plan(self.delete_plan(window))
        sde = [d for d in window.disks if d.name == "sde"][0]
        window.run_plan(diskmaintain.plan_trim(sde, sde.volume("sde1"), {}))
        self.answers = [True]
        window.apply_changes()
        self.assertEqual([r["op"] for r in self.client.requests],
                         ["part_delete", "trim"])
        self.assertEqual(self.client.requests[0]["device"], "/dev/sdb1")
        self.assertEqual(len(window.queue), 0)
        self.assertIn("both changes were made", window.status.text())

    def test_declining_the_last_question_changes_nothing(self):
        window = self.window()
        window.run_plan(self.delete_plan(window))
        self.answers = [False]
        window.apply_changes()
        self.assertEqual(self.client.requests, [])
        self.assertEqual(len(window.queue), 1)

    def test_the_first_failure_stops_the_rest(self):
        window = self.window()
        window.run_plan(self.delete_plan(window))
        sde = [d for d in window.disks if d.name == "sde"][0]
        window.run_plan(diskmaintain.plan_trim(sde, sde.volume("sde1"), {}))
        self.client.error = diskctl.HelperError("the drive said no")
        self.answers = [True]
        window.apply_changes()
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(len(window.queue), 2)
        self.assertIn("the drive said no", window.queue.changes[0].error)
        self.assertFalse(window._applying)

    def test_reading_something_prepared_is_explained_not_run(self):
        window = self.window()
        window.run_plan(self.delete_plan(window))
        sdb = [d for d in window.disks if d.name == "sdb"][0]
        window.run_plan(diskparts.plan_search(sdb, {}))
        self.assertEqual(self.client.requests, [])
        self.assertIn("Apply them first", self.messages[-1][1])

    def test_mounting_what_is_only_prepared_is_greyed(self):
        window = self.window()
        sdb = [d for d in window.disks if d.name == "sdb"][0]
        window.run_plan(diskparts.plan_create(
            dataclasses.replace(sdb, volumes=(), free=(
                disks.FreeSpace("sdb", MIB, 10 * GIB),)), MIB, GIB, "ext4",
            identity={}))
        window.queue.changes.clear()
        window.run_plan(self.delete_plan(window))
        sdb = [d for d in window.disks if d.name == "sdb"][0]
        answer = diskops.availability(diskops.EJECT, sdb)
        self.assertFalse(answer)
        self.assertIn("Changes are prepared", answer.reason)

    def test_the_panel_lists_removes_and_applies(self):
        from ksyssupervisor.rescuewindow import ChangesPanel
        window = self.window()
        panel = ChangesPanel(window)
        window.queue_listener = panel.refresh
        self.assertFalse(panel.apply.isEnabled())
        window.run_plan(self.delete_plan(window))
        self.assertEqual(panel.list.count(), 1)
        self.assertEqual(panel.apply.text(), "Apply Changes (1)")
        self.assertTrue(panel.apply.isEnabled())
        panel.list.setCurrentRow(0)
        panel._remove()
        self.assertEqual(len(window.queue), 0)
        self.assertEqual(panel.title.text(), "No changes prepared")


if __name__ == "__main__":
    unittest.main()
