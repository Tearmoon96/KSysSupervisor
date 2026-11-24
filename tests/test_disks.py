"""The disk utility's unprivileged logic: inventory, filesystems, availability.

Run against lsblk output captured from a real desktop (identifiers replaced)
plus hand-built cases for what that machine does not have: a memory stick, an
encrypted home, an old lsblk.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import GIB, MIB, FakeBlockTree  # noqa: E402

from ksyssupervisor import diskfs, diskops, disks  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "lsblk_desktop.json")


def desktop():
    with open(FIXTURE) as handle:
        return json.load(handle)


def stick(size=8 * GIB, fstype="vfat", mounted=True, label="STICK"):
    """A USB memory stick as lsblk describes one - spinning, it claims."""
    return {
        "name": "sde", "kname": "sde", "path": "/dev/sde", "type": "disk",
        "size": size, "model": "Ultra", "vendor": "SanDisk ", "tran": "usb",
        "rota": True, "rm": True, "hotplug": True, "ro": False,
        "pttype": "dos", "log-sec": 512, "phy-sec": 512, "disc-max": 0,
        "mountpoints": [None],
        "children": [{
            "name": "sde1", "kname": "sde1", "path": "/dev/sde1",
            "type": "part", "size": size - 2 * MIB, "start": 2048, "partn": 1,
            "fstype": fstype, "label": label, "uuid": "1234-ABCD",
            "parttype": "0xc", "mountpoints":
                ["/run/media/user/STICK"] if mounted else [None],
            "fsused": 1 * GIB if mounted else None,
            "fsavail": 6 * GIB if mounted else None,
        }],
    }


def installer_stick(size=128 * GIB, image=3 * GIB):
    """A stick a live image was copied onto: a disc filesystem over the
    whole device and the image's own small partitions inside it."""
    return {
        "name": "sdf", "kname": "sdf", "path": "/dev/sdf", "type": "disk",
        "size": size, "model": "Ultra", "vendor": "SanDisk ", "tran": "usb",
        "rota": True, "rm": True, "hotplug": True, "ro": False,
        "pttype": "dos", "fstype": "iso9660", "label": "Fedora-WS-Live",
        "log-sec": 512, "phy-sec": 512, "disc-max": 0,
        "mountpoints": [None],
        "children": [
            {"name": "sdf1", "kname": "sdf1", "path": "/dev/sdf1",
             "type": "part", "size": image, "start": 64, "partn": 1,
             "fstype": "iso9660", "label": "Fedora-WS-Live",
             "parttype": "0x0", "mountpoints": [None]},
            {"name": "sdf2", "kname": "sdf2", "path": "/dev/sdf2",
             "type": "part", "size": 30 * MIB, "start": 6400000, "partn": 2,
             "fstype": "vfat", "label": "ANACONDA", "parttype": "0xef",
             "mountpoints": [None]},
        ],
    }


def split_stick(size=128 * GIB):
    """What a Windows installer leaves: a data partition and a boot one."""
    entry = stick(size=size, fstype="exfat", mounted=False, label="")
    entry["name"] = entry["kname"] = "sdg"
    entry["path"] = "/dev/sdg"
    entry["pttype"] = "gpt"
    first = entry["children"][0]
    first.update(name="sdg1", kname="sdg1", path="/dev/sdg1",
                 size=size - 4 * MIB, parttype="ebd0a0a2-b9e5-4433-87c0-"
                                               "68b6b72699c7")
    entry["children"].append({
        "name": "sdg2", "kname": "sdg2", "path": "/dev/sdg2", "type": "part",
        "size": 1 * MIB, "start": (size - 3 * MIB) // 512, "partn": 2,
        "fstype": "vfat", "label": "RUFUS_BOOT",
        "parttype": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
        "mountpoints": [None]})
    return entry


def parse(*entries, fstab=()):
    return disks.parse({"blockdevices": list(entries)}, fstab=list(fstab))


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.disks = {d.name: d for d in disks.parse(desktop(), fstab=[])}

    def test_only_real_drives_are_listed(self):
        self.assertEqual(sorted(self.disks),
                         ["nvme0n1", "nvme1n1", "sda", "sdb", "sdc", "sdd"])

    def test_each_drive_is_told_apart_by_how_it_is_built(self):
        kinds = {name: disk.kind for name, disk in self.disks.items()}
        self.assertEqual(kinds, {"nvme0n1": disks.NVME, "nvme1n1": disks.NVME,
                                 "sda": disks.HDD, "sdb": disks.SSD,
                                 "sdc": disks.SSD, "sdd": disks.USB})

    def test_the_bus_name_is_not_taken_for_a_maker(self):
        self.assertEqual(self.disks["sda"].title, "ST8000DM004-2U9188")

    def test_the_system_disk_is_the_one_the_system_is_mounted_from(self):
        system = [name for name, disk in self.disks.items() if disk.is_system]
        self.assertEqual(system, ["nvme0n1"])
        mounts = sorted(m for v in self.disks["nvme0n1"].volumes
                        for m in v.system_mounts)
        self.assertEqual(mounts, ["/", "/boot", "/boot/efi", "/home"])

    def test_a_data_volume_is_mounted_but_not_system(self):
        games = self.disks["sdb"].volumes[0]
        self.assertTrue(games.mounted)
        self.assertFalse(games.is_system)
        self.assertEqual(games.mountpoint, "/mnt/giochi")
        self.assertGreater(games.used, 0)
        self.assertGreater(games.free, 0)

    def test_an_unmounted_volume_has_no_usage_to_report(self):
        windows = self.disks["sda"].volume("sda2")
        self.assertFalse(windows.mounted)
        self.assertIsNone(windows.used)
        self.assertEqual(windows.fstype, "ntfs")

    def test_a_partition_start_is_a_byte_offset(self):
        self.assertEqual(self.disks["sda"].volume("sda1").start, 2048 * 512)

    def test_another_systems_drive_is_recognised(self):
        self.assertTrue(self.disks["nvme1n1"].holds_other_system)
        self.assertFalse(self.disks["nvme0n1"].holds_other_system)
        self.assertFalse(self.disks["sdb"].holds_other_system)

    def test_volumes_are_named_by_what_they_are(self):
        titles = [v.title for v in self.disks["nvme1n1"].volumes]
        self.assertEqual(titles[:3], ["EFI System", "Microsoft reserved",
                                      "Windows11"])

    def test_an_empty_card_reader_has_no_media(self):
        self.assertFalse(self.disks["sdd"].has_media)
        self.assertTrue(self.disks["sda"].has_media)

    def test_trim_support_is_what_the_kernel_reports(self):
        self.assertTrue(self.disks["sdb"].discard)
        self.assertFalse(self.disks["sda"].discard)

    def test_a_trailing_megabyte_is_not_free_space(self):
        """Partitioning tools leave it there; nothing fits in it."""
        self.assertEqual(self.disks["sdc"].free, ())

    def test_fstab_entries_are_matched_by_id(self):
        uuid = self.disks["sdb"].volumes[0].uuid
        found = {d.name: d for d in disks.parse(
            desktop(), fstab=[("UUID=%s" % uuid.upper(), "/mnt/giochi")])}
        self.assertEqual(found["sdb"].volumes[0].fstab_mount, "/mnt/giochi")
        self.assertEqual(found["sda"].volumes[0].fstab_mount, "")


class OddDevicesTest(unittest.TestCase):
    def test_a_memory_stick_that_claims_to_spin_is_still_flash(self):
        disk = parse(stick())[0]
        self.assertEqual(disk.kind, disks.USB)
        self.assertTrue(disk.is_solid_state)
        self.assertEqual(disk.title, "SanDisk Ultra")

    def test_an_external_hard_disk_is_not_flash(self):
        entry = stick()
        entry["rm"] = False
        self.assertFalse(parse(entry)[0].is_solid_state)

    def test_a_system_volume_inside_an_encrypted_container(self):
        entry = stick(mounted=False)
        entry["children"][0].update(fstype="crypto_LUKS", children=[{
            "name": "luks-x", "kname": "dm-0", "path": "/dev/mapper/luks-x",
            "type": "crypt", "size": 7 * GIB, "fstype": "ext4",
            "mountpoints": ["/home"]}])
        disk = parse(entry)[0]
        volume = disk.volumes[0]
        self.assertFalse(volume.mountpoints)
        self.assertTrue(volume.mounted)
        self.assertTrue(volume.is_system)
        self.assertTrue(disk.is_system)
        self.assertEqual(volume.title, "STICK")

    def test_swap_is_a_system_use(self):
        entry = stick(mounted=False, fstype="swap")
        entry["children"][0]["mountpoints"] = ["[SWAP]"]
        volume = parse(entry)[0].volumes[0]
        self.assertTrue(volume.is_system)
        self.assertEqual(volume.mountpoint, "")

    def test_a_filesystem_straight_on_the_disk_is_one_whole_volume(self):
        entry = stick()
        entry.update(fstype="vfat", label="SUPER", children=[], pttype=None,
                     mountpoints=["/run/media/user/SUPER"])
        disk = parse(entry)[0]
        self.assertEqual(len(disk.volumes), 1)
        self.assertEqual(disk.volumes[0].kind, "whole")
        self.assertEqual(disk.volumes[0].path, "/dev/sde")
        self.assertEqual(disk.free, ())

    def test_unallocated_space_is_found_and_aligned(self):
        entry = stick(size=32 * GIB)
        entry["children"][0]["size"] = 8 * GIB
        entry["pttype"] = "gpt"
        disk = parse(entry)[0]
        self.assertEqual(len(disk.free), 1)
        gap = disk.free[0]
        self.assertEqual(gap.start % MIB, 0)
        self.assertEqual(gap.size % MIB, 0)
        self.assertEqual(gap.start, 8 * GIB + MIB)
        # The backup GPT header's sectors are not free.
        self.assertLess(gap.start + gap.size, 32 * GIB)

    def test_an_extended_partition_is_a_container_not_a_volume(self):
        entry = stick(size=32 * GIB)
        entry["children"] = [
            {"name": "sde1", "type": "part", "size": 30 * GIB, "start": 2048,
             "parttype": "0x5", "partn": 1, "mountpoints": [None]},
            {"name": "sde5", "type": "part", "size": 10 * GIB, "start": 4096,
             "parttype": "0x83", "partn": 5, "fstype": "ext4",
             "mountpoints": [None]},
        ]
        disk = parse(entry)[0]
        self.assertTrue(disk.volumes[0].is_extended)
        self.assertFalse(disk.volumes[0].has_filesystem)
        self.assertTrue(disk.free)

    def test_an_old_lsblk_is_asked_again_with_fewer_columns(self):
        asked = []

        class Done:
            def __init__(self, code, out=b"", err=b""):
                self.returncode, self.stdout, self.stderr = code, out, err

        def run(columns):
            asked.append(columns)
            if "MOUNTPOINTS" in columns:
                return Done(1, err=b"lsblk: unknown column: MOUNTPOINTS")
            entry = stick()
            entry["children"][0].pop("mountpoints")
            entry["children"][0]["mountpoint"] = "/mnt/x"
            return Done(0, json.dumps({"blockdevices": [entry]}).encode())

        with mock.patch.object(disks, "read_fstab", return_value=[]):
            found = disks.inventory(run)
        self.assertEqual(len(asked), 2)
        self.assertEqual(found[0].volumes[0].mountpoints, ("/mnt/x",))

    def test_lsblk_failing_outright_is_an_error_with_its_words(self):
        class Done:
            returncode, stdout, stderr = 1, b"", b"lsblk: failed to probe"
        with self.assertRaises(disks.InventoryError) as caught:
            disks.lsblk_json(lambda columns: Done())
        self.assertIn("failed to probe", str(caught.exception))

    def test_one_unreadable_device_does_not_hide_the_rest(self):
        broken = {"name": "sdz", "type": "disk", "size": 1,
                  "children": "not a list"}
        with mock.patch.object(disks.log, "exception"):
            found = parse(broken, stick())
        self.assertEqual([d.name for d in found], ["sde"])


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.tree = FakeBlockTree(self.root)
        self.tree.add_disk("sdd", 8 * GIB, diskseq=7)
        self.tree.add_partition("sdd", "sdd1", 1, 2048, 4 * GIB)
        patch = mock.patch.object(disks, "SYS_CLASS_BLOCK", self.tree.block)
        patch.start()
        self.addCleanup(patch.stop)
        import shutil
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_disk(self):
        self.assertEqual(disks.identity("sdd"), {
            "name": "sdd", "disk": "sdd", "size": 8 * GIB, "diskseq": 7,
            "start": None})

    def test_a_partition_carries_its_disks_sequence_number(self):
        self.assertEqual(disks.identity("sdd1"), {
            "name": "sdd1", "disk": "sdd", "size": 4 * GIB, "diskseq": 7,
            "start": 2048})

    def test_it_is_what_the_helper_reads_for_itself(self):
        """Both halves read sysfs; they have to read the same thing."""
        from disktree import load_helper
        module = load_helper()
        self.tree.install_helper(module)
        for name in ("sdd", "sdd1"):
            module.Helper.check_identity(module.Device(name),
                                         disks.identity(name))


class SizeTest(unittest.TestCase):
    def test_sizes_read_as_drives_are_sold(self):
        self.assertEqual(disks.format_size(8001563222016), "8.0 TB")
        self.assertEqual(disks.format_size(500107862016), "500 GB")
        self.assertEqual(disks.format_size(32 * 1000 ** 3), "32.0 GB")
        self.assertEqual(disks.format_size(0), "0 B")
        self.assertEqual(disks.format_size(None), "-")


class FilesystemChoiceTest(unittest.TestCase):
    def everything(self, *names):
        return "/usr/sbin/" + names[0]

    def test_a_small_stick_is_offered_fat32_first(self):
        disk = parse(stick(8 * GIB))[0]
        found = diskfs.choices(disk, 8 * GIB, tools=self.everything)
        self.assertEqual(found[0].filesystem.key, "vfat")
        self.assertTrue(found[0].recommended)

    def test_a_large_stick_is_offered_exfat_first(self):
        disk = parse(stick(128 * GIB))[0]
        found = diskfs.choices(disk, 128 * GIB, tools=self.everything)
        self.assertEqual(found[0].filesystem.key, "exfat")

    def test_an_internal_drive_is_offered_ext4_first(self):
        disk = [d for d in disks.parse(desktop(), fstab=[])
                if d.name == "sdb"][0]
        found = diskfs.choices(disk, disk.size, tools=self.everything)
        self.assertEqual(found[0].filesystem.key, "ext4")

    def test_a_missing_tool_greys_the_entry_and_says_what_to_install(self):
        disk = parse(stick())[0]
        found = diskfs.choices(
            disk, 8 * GIB,
            tools=lambda *names: None if "mkfs.exfat" in names else "/x")
        exfat = [c for c in found if c.filesystem.key == "exfat"][0]
        self.assertFalse(exfat.available)
        self.assertIn("exfatprogs", exfat.reason)
        self.assertEqual(len(found), len(diskfs.FILESYSTEMS))

    def test_size_limits_grey_what_cannot_be_made(self):
        disk = parse(stick(4000 * GIB))[0]
        found = {c.filesystem.key: c for c in
                 diskfs.choices(disk, 4000 * GIB, tools=self.everything)}
        self.assertFalse(found["vfat"].available)
        self.assertIn("cannot be larger", found["vfat"].reason)

        found = {c.filesystem.key: c for c in
                 diskfs.choices(disk, 100 * MIB, tools=self.everything)}
        self.assertFalse(found["xfs"].available)
        self.assertFalse(found["btrfs"].available)
        self.assertTrue(found["ext4"].available)

    def test_removable_drives_get_the_table_old_devices_read(self):
        self.assertEqual(diskfs.default_table(parse(stick())[0]), "dos")
        big = parse(stick(3000 * GIB))[0]
        self.assertEqual(diskfs.default_table(big), "gpt")
        self.assertFalse(diskfs.table_allowed(big, "dos"))
        internal = [d for d in disks.parse(desktop(), fstab=[])
                    if d.name == "sda"][0]
        self.assertEqual(diskfs.default_table(internal), "gpt")


class ClusterChoiceTest(unittest.TestCase):
    """The cluster sizes offered, and the one recommended."""

    def offered(self, fstype, size, sector=512):
        return diskfs.cluster_choice(fstype, size, sector)

    def test_windows_sizes_are_the_recommended_ones(self):
        TIB = 1024 * GIB
        for fstype, size, best in (
                ("exfat", 200 * MIB, 4096), ("exfat", 16 * GIB, 32768),
                ("exfat", 2 * TIB, 131072), ("ntfs", 100 * MIB, 4096),
                ("ntfs", 2 * TIB, 4096), ("ntfs", 20 * TIB, 8192),
                ("vfat", 100 * MIB, 1024), ("vfat", 4 * GIB, 4096),
                ("vfat", 12 * GIB, 8192), ("vfat", 30 * GIB, 16384),
                ("vfat", 500 * GIB, 32768)):
            choice = self.offered(fstype, size)
            self.assertEqual(choice.recommended, best, (fstype, size))
            self.assertIn(best, choice.sizes)

    def test_every_size_the_volume_can_have_is_offered(self):
        sizes = self.offered("exfat", 2 * 1024 * GIB).sizes
        self.assertEqual(sizes[0], 8192)      # 2^28 clusters at most
        self.assertEqual(sizes[-1], 32 * MIB)
        self.assertEqual(self.offered("ntfs", 64 * GIB).sizes[-1], 2 * MIB)
        self.assertEqual(self.offered("vfat", 64 * GIB).sizes[-1], 65536)
        for size in self.offered("exfat", 64 * GIB).sizes:
            self.assertEqual(size & (size - 1), 0)

    def test_fat32_keeps_to_the_cluster_counts_windows_reads_as_fat32(self):
        """Fewer than 65525 clusters is FAT16 to Windows, whatever mkfs.fat
        wrote; 1 GiB at 16 KiB is just below that."""
        sizes = self.offered("vfat", GIB).sizes
        self.assertIn(8192, sizes)
        self.assertNotIn(16384, sizes)

    def test_a_small_fat_volume_chooses_its_own(self):
        choice = self.offered("vfat", 40 * MIB)
        self.assertEqual(choice.sizes, ())
        self.assertEqual(choice.fixed, "Automatic")
        self.assertIn("FAT16", choice.reason)

    def test_no_cluster_is_smaller_than_a_sector(self):
        sizes = self.offered("ntfs", 64 * GIB, sector=4096).sizes
        self.assertEqual(sizes[0], 4096)
        self.assertEqual(self.offered("exfat", 64 * GIB, 4096).recommended,
                         131072)

    def test_linux_filesystems_are_shown_their_4_kib(self):
        for fstype in ("ext4", "btrfs", "xfs", "f2fs"):
            choice = self.offered(fstype, 64 * GIB)
            self.assertEqual((choice.sizes, choice.fixed), ((), "4 KiB"))
            self.assertIn("memory pages", choice.reason)
        self.assertIn("inside its own records",
                      self.offered("btrfs", 64 * GIB).reason)

    def test_the_note_says_what_a_choice_costs(self):
        note = diskfs.cluster_note("exfat", 131072, 131072)
        self.assertIn("even a 1\u00a0KB file uses 128\u00a0KiB", note)
        self.assertIn("usual size", note)
        self.assertIn("waste a lot of space",
                      diskfs.cluster_note("exfat", MIB, 131072))
        self.assertIn("Smaller than usual",
                      diskfs.cluster_note("exfat", 4096, 131072))
        self.assertIn("cannot compress",
                      diskfs.cluster_note("ntfs", 8192, 4096))
        self.assertIn("Windows 10 version 1709",
                      diskfs.cluster_note("ntfs", 128 * 1024, 4096))
        self.assertNotIn("1709", diskfs.cluster_note("ntfs", 65536, 4096))
        self.assertIn("older devices",
                      diskfs.cluster_note("vfat", 65536, 32768))

    def test_sizes_read_as_people_know_them(self):
        self.assertEqual(diskfs.format_cluster(512), "512 bytes")
        self.assertEqual(diskfs.format_cluster(65536), "64 KiB")
        self.assertEqual(diskfs.format_cluster(MIB), "1 MiB")


class CurrentClusterTest(unittest.TestCase):
    """A mounted Windows filesystem shows the cluster size it has."""

    def setUp(self):
        self.asked = []

        def statvfs(path):
            self.asked.append(path)
            if path != "/run/media/user/STICK":
                raise OSError(2, "gone")
            return os.statvfs_result((MIB,) * 10)

        patcher = mock.patch.object(disks, "statvfs", statvfs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_mounted_exfat_volume_has_its_size(self):
        volume = parse(stick(fstype="exfat"))[0].volumes[0]
        self.assertEqual(volume.cluster, MIB)

    def test_unmounted_or_linux_volumes_are_not_asked(self):
        self.assertIsNone(parse(stick(fstype="exfat", mounted=False))[0]
                          .volumes[0].cluster)
        self.assertIsNone(parse(stick(fstype="ext4"))[0].volumes[0].cluster)
        self.assertEqual(self.asked, [])

    def test_a_mount_that_cannot_be_read_is_unknown(self):
        self.assertIsNone(disks.cluster_size("ntfs", ("/somewhere/else",)))


class LabelTest(unittest.TestCase):
    def check(self, key, text):
        return diskfs.validate_label(diskfs.FILESYSTEMS[key], text)

    def test_fat_names_are_upper_case_and_eleven_long(self):
        self.assertEqual(self.check("vfat", " photos "), ("PHOTOS", None))
        self.assertIsNone(self.check("vfat", "A" * 11)[1])
        self.assertIn("Too long", self.check("vfat", "A" * 12)[1])
        self.assertIn(".", self.check("vfat", "my.disk")[1])

    def test_length_is_counted_the_way_the_filesystem_counts(self):
        # Four characters, twelve bytes: fits ext4's 16 bytes, not twice.
        self.assertIsNone(self.check("ext4", "日本語の")[1])
        self.assertIsNotNone(self.check("ext4", "日本語の日本")[1])
        # NTFS counts UTF-16 units, so the same text has room to spare.
        self.assertIsNone(self.check("ntfs", "日本語の日本")[1])
        self.assertIsNotNone(self.check("xfs", "thirteen char")[1])


class AdviceTest(unittest.TestCase):
    def titles(self, fs, disk, size, volume=None, **kwargs):
        return [a.title for a in diskfs.advise(
            diskfs.FILESYSTEMS[fs], disk, size, volume, **kwargs)]

    def test_ntfs_on_an_eight_gigabyte_stick(self):
        disk = parse(stick(8 * GIB))[0]
        advice = diskfs.advise(diskfs.FILESYSTEMS["ntfs"], disk, 8 * GIB)
        self.assertEqual(advice[0].level, diskfs.WARNING)
        self.assertIn("small removable drive", advice[0].title)
        self.assertIn("exFAT", advice[0].text)

    def test_ntfs_on_an_internal_drive_is_nobodys_business(self):
        disk = [d for d in disks.parse(desktop(), fstab=[])
                if d.name == "sdc"][0]
        self.assertEqual(self.titles("ntfs", disk, disk.size), [])

    def test_a_linux_filesystem_on_a_stick_is_invisible_elsewhere(self):
        disk = parse(stick())[0]
        self.assertIn("Only Linux will be able to read this drive",
                      self.titles("ext4", disk, 8 * GIB))

    def test_fat32_says_what_will_not_fit(self):
        disk = parse(stick(64 * GIB))[0]
        titles = self.titles("vfat", disk, 64 * GIB)
        self.assertIn("Files larger than 4 GB will not fit", titles)
        self.assertIn("Windows cannot create FAT32 this large", titles)

    def test_a_volume_fstab_mounts_is_flagged(self):
        entry = stick(fstype="ext4")
        disk = parse(entry, fstab=[("UUID=1234-ABCD", "/mnt/data")])[0]
        volume = disk.volumes[0]
        self.assertEqual(volume.fstab_mount, "/mnt/data")
        titles = self.titles("exfat", disk, volume.size, volume)
        self.assertIn("This volume is mounted automatically at startup",
                      titles)
        # Same type again and the ID carried over: the line stays valid.
        fs = diskfs.FILESYSTEMS["ext4"]
        self.assertTrue(diskfs.can_keep_identity(fs, volume))
        titles = self.titles("ext4", disk, volume.size, volume,
                             keeps_identity=True)
        self.assertIn("Automatic mounting keeps working", titles)
        self.assertFalse(diskfs.can_keep_identity(
            diskfs.FILESYSTEMS["exfat"], volume))


class AvailabilityTest(unittest.TestCase):
    """What is greyed out, and what the tooltip says about why."""

    def setUp(self):
        self.disks = {d.name: d for d in disks.parse(desktop(), fstab=[])}
        self.stick = parse(stick())[0]

    def can(self, action, disk, volume=None):
        return diskops.availability(action, disk, volume,
                                    tools=lambda *names: "/x")

    def test_the_system_disk_cannot_be_formatted(self):
        answer = self.can(diskops.FORMAT, self.disks["nvme0n1"])
        self.assertFalse(answer)
        self.assertIn("running system", answer.reason)
        self.assertIn("/boot/efi", answer.reason)

    def test_no_system_volume_offers_anything_destructive(self):
        disk = self.disks["nvme0n1"]
        for volume in disk.volumes:
            for action in (diskops.FORMAT, diskops.UNMOUNT, diskops.RENAME):
                answer = self.can(action, disk, volume)
                self.assertFalse(answer, (volume.name, action))
                self.assertIn("running system", answer.reason)

    def test_a_data_volume_offers_everything(self):
        disk = self.disks["sdb"]
        volume = disk.volumes[0]
        self.assertTrue(self.can(diskops.FORMAT, disk))
        self.assertTrue(self.can(diskops.FORMAT, disk, volume))
        self.assertTrue(self.can(diskops.UNMOUNT, disk, volume))
        self.assertTrue(self.can(diskops.RENAME, disk, volume))
        answer = self.can(diskops.MOUNT, disk, volume)
        self.assertFalse(answer)
        self.assertIn("/mnt/giochi", answer.reason)

    def test_an_unmounted_volume_can_be_mounted_not_unmounted(self):
        disk = self.disks["sda"]
        volume = disk.volume("sda2")
        self.assertTrue(self.can(diskops.MOUNT, disk, volume))
        self.assertFalse(self.can(diskops.UNMOUNT, disk, volume))

    def test_an_internal_drive_is_not_ejected(self):
        self.assertFalse(self.can(diskops.EJECT, self.disks["sda"]))
        self.assertTrue(self.can(diskops.EJECT, self.stick))

    def test_an_empty_card_reader_offers_nothing(self):
        for action in (diskops.FORMAT, diskops.EJECT):
            answer = self.can(action, self.disks["sdd"])
            self.assertFalse(answer)
            self.assertIn("no media", answer.reason)

    def test_a_partition_without_a_filesystem(self):
        disk = self.disks["nvme1n1"]
        reserved = disk.volume("nvme1n1p2")
        self.assertFalse(self.can(diskops.MOUNT, disk, reserved))
        self.assertFalse(self.can(diskops.RENAME, disk, reserved))
        self.assertTrue(self.can(diskops.FORMAT, disk, reserved))

    def test_a_missing_label_tool_says_what_to_install(self):
        disk = self.disks["sda"]
        answer = diskops.availability(diskops.RENAME, disk,
                                      disk.volume("sda2"),
                                      tools=lambda *names: None)
        self.assertFalse(answer)
        self.assertIn("ntfslabel", answer.reason)
        self.assertIn("ntfs-3g", answer.reason)

    def test_an_open_encrypted_volume_blocks_formatting_underneath(self):
        entry = stick(mounted=False)
        entry["children"][0].update(fstype="crypto_LUKS", children=[{
            "name": "luks-x", "kname": "dm-0", "type": "crypt",
            "size": 7 * GIB, "fstype": "ext4", "mountpoints": [None]}])
        disk = parse(entry)[0]
        for volume in (None, disk.volumes[0]):
            answer = self.can(diskops.FORMAT, disk, volume)
            self.assertFalse(answer)
            self.assertIn("Close that first", answer.reason)

    def test_every_refusal_carries_a_reason(self):
        for disk in list(self.disks.values()) + [self.stick]:
            for volume in (None,) + disk.volumes:
                for action in (diskops.FORMAT, diskops.MOUNT, diskops.UNMOUNT,
                               diskops.EJECT, diskops.RENAME):
                    answer = self.can(action, disk, volume)
                    if not answer:
                        self.assertTrue(answer.reason.strip(),
                                        (disk.name, volume, action))


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.disks = {d.name: d for d in disks.parse(desktop(), fstab=[])}
        self.stick = parse(stick())[0]

    def test_formatting_a_stick(self):
        plan = diskops.plan_format(self.stick, None, "exfat", "Holiday",
                                   identity={"name": "sde"})
        self.assertEqual(plan.request["device"], "/dev/sde")
        self.assertEqual(plan.request["table"], "dos")
        self.assertEqual(plan.request["erase"], "none")
        self.assertEqual(plan.request["label"], "Holiday")
        self.assertFalse(plan.request["owner"])
        self.assertEqual(plan.step_keys,
                         ["unmount", "erase", "table", "mkfs", "verify"])
        self.assertEqual(len(plan.steps), len(plan.step_keys))
        self.assertFalse(plan.internal)
        self.assertTrue(plan.remount)
        self.assertIn("STICK", plan.loses[0])
        self.assertIn("1.1 GB of data", plan.loses[0])

    def test_a_full_format_of_a_hard_disk_writes_zeros(self):
        disk = self.disks["sda"]
        plan = diskops.plan_format(disk, disk.volume("sda2"), "ntfs", "D",
                                   quick=False, identity={})
        self.assertEqual(plan.request["erase"], "zero")
        self.assertIn("zeros", plan.steps[0])
        self.assertTrue(plan.internal)

    def test_a_full_format_of_an_ssd_discards_instead(self):
        disk = self.disks["sdc"]
        plan = diskops.plan_format(disk, disk.volumes[0], "ext4", "",
                                   quick=False, identity={})
        self.assertEqual(plan.request["erase"], "discard")
        self.assertTrue(plan.request["owner"])

    def test_a_stick_is_never_discarded(self):
        """Its bridge takes no TRIM; zeros are the only full erase there."""
        plan = diskops.plan_format(self.stick, None, "vfat", "", quick=False,
                                   identity={})
        self.assertEqual(plan.request["erase"], "zero")

    def test_what_is_lost_names_another_system(self):
        disk = self.disks["nvme1n1"]
        plan = diskops.plan_format(disk, None, "ext4", "", identity={})
        self.assertIn("operating system", plan.loses[0])
        self.assertEqual(len(plan.loses), 1 + len(disk.volumes))

    def test_a_bad_label_never_becomes_a_plan(self):
        with self.assertRaises(ValueError):
            diskops.plan_format(self.stick, None, "vfat", "far too long name",
                                identity={})

    def test_reformatting_as_the_same_type_keeps_the_id(self):
        volume = self.disks["sdb"].volumes[0]
        plan = diskops.plan_format(self.disks["sdb"], volume, "ext4", "G",
                                   identity={})
        self.assertEqual(plan.request["uuid"], volume.uuid)
        plan = diskops.plan_format(self.disks["sdb"], volume, "xfs", "G",
                                   identity={})
        self.assertIsNone(plan.request["uuid"])

    def test_renaming_a_mounted_volume_puts_it_back(self):
        disk = self.disks["sdb"]
        plan = diskops.plan_rename(disk, disk.volumes[0], "Steam",
                                   identity={})
        self.assertEqual(plan.request["op"], "label")
        self.assertEqual(plan.step_keys, ["unmount", "label", "verify"])
        self.assertTrue(plan.remount)
        self.assertEqual(plan.steps[-1], "Mount it again")


class SplitDriveTest(unittest.TestCase):
    """A drive that is not one piece: what is known and said about it."""

    def test_a_stick_carrying_an_installer_image_is_recognised(self):
        disk = parse(installer_stick())[0]
        self.assertTrue(disk.written_as_image)
        self.assertEqual(disk.fstype, "iso9660")
        self.assertEqual([v.name for v in disk.partitions], ["sdf1", "sdf2"])
        # Most of it is outside what the image laid out.
        self.assertGreater(disk.unallocated, 100 * GIB)

    def test_an_ordinary_stick_is_not_an_image(self):
        for entry in (stick(), split_stick()):
            self.assertFalse(parse(entry)[0].written_as_image)
        self.assertFalse(any(d.written_as_image
                             for d in disks.parse(desktop(), fstab=[])))

    def test_formatting_one_partition_of_an_image_stick_says_so(self):
        disk = parse(installer_stick())[0]
        advice = diskfs.advise(diskfs.FILESYSTEMS["exfat"], disk,
                               disk.volumes[0].size, disk.volumes[0])
        titles = [a.title for a in advice]
        self.assertIn("This drive was written as an installer image", titles)
        text = [a.text for a in advice if "installer" in a.title][0]
        self.assertIn("Whole drive", text)
        self.assertIn(disks.format_size(disk.size), text)

    def test_formatting_one_of_two_partitions_says_so(self):
        disk = parse(split_stick())[0]
        advice = diskfs.advise(diskfs.FILESYSTEMS["exfat"], disk,
                               disk.volumes[0].size, disk.volumes[0])
        text = [a.text for a in advice if a.title == "This drive is split up"]
        self.assertEqual(len(text), 1)
        self.assertIn("2 partitions", text[0])
        self.assertIn("Whole drive", text[0])

    def test_a_stick_in_one_piece_gets_no_such_advice(self):
        disk = parse(stick())[0]
        self.assertIsNone(diskfs.split_up(disk))

    def test_the_whole_drive_format_is_not_told_to_choose_itself(self):
        for entry in (installer_stick(), split_stick()):
            disk = parse(entry)[0]
            advice = diskfs.advise(diskfs.FILESYSTEMS["exfat"], disk,
                                   disk.size - 2 * MIB, None)
            self.assertFalse([a for a in advice if "Whole drive" in a.text])

    def test_internal_drives_are_not_nagged_about_their_partitions(self):
        machine = {d.name: d for d in disks.parse(desktop(), fstab=[])}
        disk = machine["sda"]
        advice = diskfs.advise(diskfs.FILESYSTEMS["ext4"], disk,
                               disk.volumes[1].size, disk.volumes[1])
        self.assertFalse([a for a in advice if "split up" in a.title])

    def test_a_filesystem_on_the_bare_drive_formats_as_the_drive(self):
        entry = stick()
        entry.update(pttype=None, fstype="vfat", children=[])
        disk = parse(entry)[0]
        whole = disk.volumes[0]
        self.assertEqual(whole.kind, "whole")
        self.assertTrue(diskops.availability(diskops.FORMAT, disk, whole))


if __name__ == "__main__":
    unittest.main()
