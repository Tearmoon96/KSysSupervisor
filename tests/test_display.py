"""Display layer tests. No Qt, no hardware: the module is pure by design."""

import unittest

from ksyssupervisor import display
from ksyssupervisor.display import (History, Metric, Staleness, TileBuilder,
                                    collapse_series, format_pair,
                                    format_value, fraction_of, short_label)
from ksyssupervisor.providers.base import Device, Reading


CPU = Device("cpu", "Ryzen 7 9700X", order=10)
RAM = Device("ram", "RAM", order=20)
GPU = Device("gpu:0000:03:00.0", "Radeon RX 7800 XT", order=30)
DISK = Device("disk:nvme0", "Samsung 990 Pro", order=40)


def reading(device, group, key, label, value, kind, total=None):
    return Reading(device, group, key, label, value, kind, total=total)


def cpu_sample(load=42.0, temp=61.5, watts=88.0, cores=0):
    out = [reading("cpu", "Utilization", "usage_total", "Processor", load,
                   "utilization"),
           reading("cpu", "Temperatures", "Tctl", "Package", temp, "temp")]
    if watts is not None:
        out.append(reading("cpu", "Powers", "rapl_Package", "Package", watts,
                           "power"))
    for i in range(cores):
        out.append(reading("cpu", "Clocks", "clock_%d" % i, "Core %d" % i,
                           4000.0 + i, "clock"))
    return out


def builder(*devices, grace=None):
    return TileBuilder({d.key: d for d in devices}, grace=grace)


def by_key(tiles):
    return {t.key: t for t in tiles}


class FormattingTest(unittest.TestCase):
    def test_decimals_are_per_kind(self):
        self.assertEqual(format_value(61.48, "temp"), "61.5 °C")
        self.assertEqual(format_value(1240.4, "fan"), "1240 RPM")
        self.assertEqual(format_value(4200.0, "clock"), "4200 MHz")
        self.assertEqual(format_value(1.2345, "voltage"), "1.234 V")
        self.assertEqual(format_value(47.6, "utilization"), "48 %")

    def test_absent_and_unusable_values_render_as_missing(self):
        self.assertEqual(format_value(None, "temp"), display.MISSING)
        self.assertEqual(format_value("warm", "temp"), display.MISSING)

    def test_an_unknown_kind_keeps_the_number_and_drops_the_unit(self):
        self.assertEqual(format_value(3.0, "photons"), "3.0")

    def test_a_pair_states_its_ceiling(self):
        self.assertEqual(format_pair(12.4, 32.0, "memory_gb"), "12.4 / 32.0 GB")

    def test_an_amount_is_rounded_to_what_its_total_deserves(self):
        self.assertEqual(format_pair(1.64, 16.0, "memory_gb"), "1.6 / 16.0 GB")
        self.assertEqual(format_pair(776.2, 983.3, "memory_gb"),
                         "776 / 983 GB")
        self.assertEqual(format_pair(11.9, 3936.0, "memory_gb"),
                         "12 GB / 3.9 TB")
        self.assertEqual(format_pair(3200.0, 3936.0, "memory_gb"),
                         "3.2 / 3.9 TB")

    def test_a_pair_without_a_ceiling_is_just_the_value(self):
        self.assertEqual(format_pair(12.4, None, "memory_gb"), "12.4 GB")
        self.assertEqual(format_pair(12.4, 0, "memory_gb"), "12.4 GB")


class ShortLabelTest(unittest.TestCase):
    def test_prose_labels_are_shortened(self):
        self.assertEqual(short_label("Processor"), "Load")
        self.assertEqual(short_label("Memory Used"), "Used")
        self.assertEqual(short_label("Power Limit"), "Limit")

    def test_gpu_resolves_against_the_kind_it_arrived_with(self):
        self.assertEqual(short_label("GPU", "utilization"), "Load")
        self.assertEqual(short_label("GPU", "temp"), "Edge")

    def test_driver_raw_names_are_left_alone(self):
        for raw in ("in0", "AUXTIN1", "Composite", "Sensor 1"):
            self.assertEqual(short_label(raw), raw)


class FractionTest(unittest.TestCase):
    def test_a_total_gives_the_share_of_it(self):
        self.assertAlmostEqual(fraction_of(8.0, 32.0, "memory_gb"), 0.25)

    def test_a_percentage_is_its_own_ceiling(self):
        self.assertAlmostEqual(fraction_of(47.0, None, "utilization"), 0.47)

    def test_a_plain_reading_has_no_bar(self):
        self.assertIsNone(fraction_of(61.5, None, "temp"))

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(fraction_of(140.0, None, "utilization"), 1.0)
        self.assertEqual(fraction_of(-5.0, None, "utilization"), 0.0)


class HistoryTest(unittest.TestCase):
    def test_the_first_sample_is_both_bounds(self):
        history = History()
        self.assertEqual(history.update("cpu/Tctl", 50.0), (50.0, 50.0))

    def test_bounds_widen_and_never_narrow(self):
        history = History()
        for value in (50.0, 70.0, 40.0, 60.0):
            history.update("cpu/Tctl", value)
        self.assertEqual(history.bounds("cpu/Tctl"), (40.0, 70.0))

    def test_a_missing_reading_does_not_move_the_bounds(self):
        history = History()
        history.update("cpu/Tctl", 50.0)
        history.update("cpu/Tctl", None)
        self.assertEqual(history.bounds("cpu/Tctl"), (50.0, 50.0))

    def test_clearing_forgets_everything(self):
        history = History()
        history.update("cpu/Tctl", 50.0)
        history.clear()
        self.assertIsNone(history.bounds("cpu/Tctl"))


class StalenessTest(unittest.TestCase):
    """A sensor that stops reporting must not keep showing its last value.

    Moved here from the window: this is the same regression the tree guarded,
    now that the rule lives somewhere it can be tested without a QMainWindow.
    """

    def test_a_row_goes_stale_only_after_the_grace_period(self):
        staleness = Staleness()
        known = ["cpu/tctl", "disk:sda/temp1"]

        self.assertEqual(staleness.mark({"cpu/tctl"}, known), set())
        for _ in range(Staleness.GRACE - 2):
            self.assertEqual(staleness.mark({"cpu/tctl"}, known), set())
        self.assertEqual(staleness.mark({"cpu/tctl"}, known), {"disk:sda/temp1"})

    def test_a_returning_sensor_resets_its_counter(self):
        staleness = Staleness()
        known = ["cpu/tctl"]
        staleness.mark(set(), known)
        staleness.mark({"cpu/tctl"}, known)
        for _ in range(Staleness.GRACE - 1):
            self.assertEqual(staleness.mark(set(), known), set())


class MetricTest(unittest.TestCase):
    def test_metrics_compare_by_value_so_a_widget_can_skip_one(self):
        first = TileBuilder()._metric(
            reading("cpu", "Temperatures", "Tctl", "Package", 61.5, "temp"))
        second = TileBuilder()._metric(
            reading("cpu", "Temperatures", "Tctl", "Package", 61.5, "temp"))
        self.assertEqual(first, second)


class TileBuildingTest(unittest.TestCase):
    def test_devices_come_back_in_their_declared_order(self):
        build = builder(GPU, CPU, RAM)
        tiles = build.build(cpu_sample() + [
            reading("ram", "Utilization", "usage", "Memory Used", 12.4,
                    "memory_gb", total=32.0),
            reading(GPU.key, "Utilization", "usage_total", "GPU", 8.0,
                    "utilization"),
        ])
        self.assertEqual([t.key for t in tiles], ["cpu", "ram", GPU.key])

    def test_a_custom_name_replaces_the_device_name(self):
        build = builder(CPU)
        tiles = build.build(cpu_sample(), names={"cpu": "The Big One"})
        self.assertEqual(tiles[0].name, "The Big One")

    def test_the_cpu_rings_its_load_and_chips_the_rest(self):
        build = builder(CPU)
        tile = build.build(cpu_sample(cores=4) + [reading(
            "cpu", "Voltages", "SVI2_Core", "Core", 1.1, "voltage")])[0]
        self.assertEqual([m.uid for m in tile.rings], ["cpu/usage_total"])
        self.assertEqual(tile.locked, ("cpu/usage_total",))
        # Beside the ring the hot spot and the clock; under it the voltage
        # and the wattage.
        self.assertEqual([m.label for m in tile.chips],
                         ["Hot Spot", "Avg clock", "Voltage", "Package"])
        self.assertEqual([m.text for m in tile.chips],
                         ["61.5 °C", "4002 MHz", "1.100 V", "88.0 W"])

    def test_the_cpu_borrows_the_boards_vcore(self):
        board = Device("mobo", "Board", order=60)
        build = builder(CPU, board)
        tiles = by_key(build.build(cpu_sample() + [
            reading("mobo", "Voltages", "in0", "Vcore", 1.25, "voltage"),
            reading("mobo", "Voltages", "in1", "Rail 1", 1.8, "voltage")]))
        voltage = [m for m in tiles["cpu"].chips if m.kind == "voltage"]
        self.assertEqual([(m.label, m.text) for m in voltage],
                         [("Voltage", "1.250 V")])

    def test_an_unnamed_rail_is_not_claimed_for_the_cpu(self):
        board = Device("mobo", "Board", order=60)
        build = builder(CPU, board)
        tiles = by_key(build.build(cpu_sample() + [
            reading("mobo", "Voltages", "in0", "Rail 0", 1.25, "voltage")]))
        self.assertNotIn("voltage", [m.kind for m in tiles["cpu"].chips])

    def test_the_hot_spot_is_the_hottest_die_sensor(self):
        build = builder(CPU)
        tile = build.build(cpu_sample(temp=50.0) + [
            reading("cpu", "Temperatures", "Tccd1", "Core (CCD1)", 58.0,
                    "temp")])[0]
        self.assertEqual(tile.chips[0].text, "58.0 °C")

    def test_an_offset_tctl_is_not_the_hot_spot(self):
        """k10temp adds Tdie only where Tctl carries a fan-curve offset."""
        build = builder(CPU)
        tile = build.build(cpu_sample(temp=80.0) + [
            reading("cpu", "Temperatures", "Tdie", "Core", 60.0, "temp")])[0]
        self.assertEqual(tile.chips[0].text, "60.0 °C")

    def ram(self, *slots):
        build = builder(RAM)
        return build.build([
            reading("ram", "Utilization", "usage", "Memory Used", 8.0,
                    "memory_gb", total=32.0)] + [
            reading("ram", "Temperatures", "d%d" % n, "DIMM %d" % n, 35.0,
                    "temp") for n in slots])[0]

    def test_ram_rings_its_use_and_chips_every_stick(self):
        from unittest import mock
        with mock.patch.object(display.memory, "_slot_count", 2):
            tile = self.ram(2, 1)
        self.assertEqual([m.text for m in tile.rings], ["8.0 / 32.0 GB"])
        self.assertEqual(tile.locked, ("ram/usage",))
        self.assertEqual([m.label for m in tile.chips], ["DIMM 1", "DIMM 2"])
        self.assertEqual(tile.rows, ())

    def test_four_slots_put_the_first_filled_beside_the_ring(self):
        """2 and 4 beside the ring, 1 and 3 on the line under it."""
        from unittest import mock
        with mock.patch.object(display.memory, "_slot_count", 4):
            tile = self.ram(1, 2, 3, 4)
        self.assertEqual([m.label for m in tile.chips],
                         ["DIMM 2", "DIMM 4", "DIMM 1", "DIMM 3"])

    def test_a_card_rings_its_load_and_memory_and_chips_the_rest(self):
        build = builder(GPU)
        tile = build.build([
            reading(GPU.key, "Utilization", "usage_total", "GPU", 8.0,
                    "utilization"),
            reading(GPU.key, "Utilization", "PPT_pct", "Power Limit", 20.0,
                    "utilization"),
            reading(GPU.key, "VRAM", "vram", "VRAM", 2.0, "memory_gb",
                    total=16.0),
            reading(GPU.key, "Clocks", "sclk", "Graphics", 2400.0, "clock"),
            reading(GPU.key, "Clocks", "mclk", "Memory", 1000.0, "clock"),
            reading(GPU.key, "Voltages", "vddgfx", "GPU", 0.8, "voltage"),
            reading(GPU.key, "Powers", "PPT", "Power", 200.0, "power"),
            reading(GPU.key, "Temperatures", "junction", "Hot Spot", 70.0,
                    "temp")])[0]
        self.assertEqual([m.label for m in tile.rings], ["Load", "VRAM"])
        self.assertEqual(len(tile.locked), 2)
        # Hot spot and clock beside the load; voltage and power beside the
        # memory.
        self.assertEqual([m.label for m in tile.chips],
                         ["Hot Spot", "Clock", "Voltage", "Power"])
        self.assertEqual([m.label for m in tile.rows], ["Limit"])

    def test_a_card_without_a_load_reading_has_no_load_ring(self):
        """Not its power-limit percentage standing in for one."""
        build = builder(GPU)
        tile = build.build([
            reading(GPU.key, "Utilization", "PPT_pct", "Power Limit", 20.0,
                    "utilization")])[0]
        self.assertEqual(tile.rings, ())

    def test_a_drive_rings_its_volumes(self):
        build = builder(DISK)
        tile = build.build([
            reading(DISK.key, "Temperatures", "Composite", "Composite", 38.0,
                    "temp"),
            reading(DISK.key, "Utilization", "vol:/boot", "boot", 0.5,
                    "memory_gb", total=2.0),
            reading(DISK.key, "Utilization", "vol:/home", "home", 400.0,
                    "memory_gb", total=2000.0),
            reading(DISK.key, "Utilization", "vol:/", "System", 40.0,
                    "memory_gb", total=100.0)])[0]
        self.assertEqual([m.label for m in tile.rings],
                         ["System", "home", "boot"])
        self.assertEqual([m.text for m in tile.chips], ["38.0 °C"])

    def test_a_headline_slot_is_held_open_before_its_first_reading(self):
        """RAPL is a delta counter: no wattage exists on the first tick."""
        build = builder(CPU)
        tile = build.build(cpu_sample(watts=None))[0]
        self.assertEqual(len(tile.chips), 2)
        power = tile.chips[1]
        self.assertEqual(power.kind, "power")
        self.assertEqual(power.text, display.MISSING)

        # And the slot keeps its place once the reading does arrive.
        tile = build.build(cpu_sample(watts=88.0))[0]
        self.assertEqual(len(tile.chips), 2)
        self.assertEqual(tile.chips[1].text, "88.0 W")

    def test_headline_readings_are_captioned(self):
        """Two temperatures side by side are not told apart by their unit."""
        build = builder(CPU)
        tile = build.build(cpu_sample())[0]
        self.assertEqual([m.label for m in tile.headline],
                         ["Load", "Hot Spot", "Package"])

    def test_a_waiting_slot_is_captioned_by_what_it_waits_for(self):
        build = builder(CPU)
        tile = build.build(cpu_sample(watts=None))[0]
        self.assertEqual(tile.chips[1].label, "Power")
        self.assertEqual(tile.chips[1].text, display.MISSING)

    def test_a_card_leads_with_its_hot_spot_not_its_edge(self):
        build = builder(GPU)
        tile = build.build([
            reading(GPU.key, "Temperatures", "edge", "GPU", 47.0, "temp"),
            reading(GPU.key, "Temperatures", "junction", "Hot Spot", 63.0,
                    "temp"),
            reading(GPU.key, "Temperatures", "mem", "Memory", 52.0, "temp"),
        ])[0]
        self.assertEqual(tile.chips[0].text, "63.0 °C")

    def test_a_headline_reading_is_not_repeated_in_the_body(self):
        build = builder(CPU)
        tile = build.build(cpu_sample())[0]
        headline = {m.uid for m in tile.headline}
        self.assertFalse(headline & {m.uid for m in tile.rows})

    def test_a_reading_with_a_ceiling_carries_a_bar(self):
        build = builder(RAM)
        tile = build.build([reading("ram", "Utilization", "usage",
                                    "Memory Used", 8.0, "memory_gb",
                                    total=32.0)])[0]
        used = tile.headline[0]
        self.assertEqual(used.text, "8.0 / 32.0 GB")
        self.assertAlmostEqual(used.fraction, 0.25)
        # Min/max would only restate a ceiling the value already names.
        self.assertEqual((used.lo, used.hi), (display.MISSING, display.MISSING))

    @staticmethod
    def _package(tile):
        return next(m for m in tile.rows if m.uid == "cpu/Tctl")

    def test_min_and_max_follow_the_readings(self):
        """Still kept for the tree, which shows them; a tile does not."""
        build = builder(CPU)
        build.build(cpu_sample(temp=50.0))
        build.build(cpu_sample(temp=70.0))
        package = self._package(build.build(cpu_sample(temp=60.0))[0])
        self.assertEqual((package.lo, package.hi), ("50.0 °C", "70.0 °C"))

    def test_clearing_the_bounds_restarts_them(self):
        build = builder(CPU)
        build.build(cpu_sample(temp=90.0))
        build.clear_bounds()
        build.build(cpu_sample(temp=60.0))
        package = self._package(build.build(cpu_sample(temp=65.0))[0])
        # The 90 is gone; the range starts again from what came after it.
        self.assertEqual((package.lo, package.hi), ("60.0 °C", "65.0 °C"))

    def test_a_reading_that_has_not_moved_shows_no_range(self):
        """Otherwise every idle row reads "49.1 °C  49.1 / 49.1 °C"."""
        build = builder(CPU)
        package = self._package(build.build(cpu_sample(temp=61.5))[0])
        self.assertEqual((package.lo, package.hi),
                         (display.MISSING, display.MISSING))

    def test_clocks_and_voltages_are_detail_not_body(self):
        build = builder(CPU)
        tile = build.build(cpu_sample(cores=8))[0]
        # The body may carry the summary row for the group, but never one of
        # the individual readings that went into it.
        individual = [m for m in tile.rows if not m.key.startswith("!")]
        self.assertNotIn("Clocks", {m.group for m in individual})
        self.assertIn("Clocks", dict(tile.detail))
        self.assertEqual(len(dict(tile.detail)["Clocks"]), 8)

    def test_a_long_run_of_readings_earns_an_average(self):
        build = builder(CPU)
        tile = build.build(cpu_sample(cores=8))[0]
        summary = [m for m in tile.chips if m.key == "!Clocks"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].label, "Avg clock")
        self.assertEqual(summary[0].text, "4004 MHz")

    def test_a_short_run_is_left_as_it_is(self):
        build = builder(CPU)
        tile = build.build(cpu_sample(cores=2))[0]
        self.assertEqual([m for m in tile.rows if m.label.startswith("Clocks")],
                         [])

    def test_the_body_does_not_grow_without_limit(self):
        build = builder(CPU)
        extra = [reading("cpu", "Fans", "fan%d" % i, "Fan %d" % i, 900.0 + i,
                         "fan") for i in range(12)]
        tile = build.build(cpu_sample() + extra)[0]
        self.assertLessEqual(len(tile.rows), display.BODY_MAX)
        self.assertTrue(tile.detail)

    def test_a_hidden_group_is_gone_from_the_tile_entirely(self):
        build = builder(CPU)
        visible = {"Clocks": False, "Temperatures": True, "Utilization": True,
                   "Powers": True}
        tile = build.build(cpu_sample(cores=8), group_visible=visible)[0]
        self.assertNotIn("Clocks", dict(tile.detail))
        self.assertFalse([m for m in tile.rows if m.group == "Clocks"])

    def test_hiding_a_group_does_not_make_its_sensors_look_stale(self):
        build = builder(CPU)
        hidden = {"Temperatures": False}
        for _ in range(Staleness.GRACE + 2):
            build.build(cpu_sample(), group_visible=hidden)
        tile = build.build(cpu_sample())[0]
        self.assertEqual(tile.chips[0].text, "61.5 °C")

    def test_a_sensor_that_stops_reporting_is_blanked_but_kept(self):
        build = builder(CPU, DISK)
        disk = [reading(DISK.key, "Temperatures", "Composite", "Composite",
                        38.0, "temp")]
        build.build(cpu_sample() + disk)

        for _ in range(Staleness.GRACE - 1):
            tiles = by_key(build.build(cpu_sample()))
            self.assertEqual(tiles[DISK.key].headline[0].text, "38.0 °C")

        tiles = by_key(build.build(cpu_sample()))
        self.assertEqual(tiles[DISK.key].headline[0].text, display.MISSING)
        self.assertEqual(tiles["cpu"].chips[0].text, "61.5 °C")

    def test_a_one_reading_device_says_it_is_headline_only(self):
        """The widget renders these compact rather than padding them out."""
        build = builder(DISK)
        tile = build.build([reading(DISK.key, "Temperatures", "Composite",
                                    "Composite", 38.0, "temp")])[0]
        self.assertEqual(tile.headline[0].text, "38.0 °C")
        self.assertTrue(tile.headline_only)
        self.assertFalse(tile.empty)

    def test_a_device_nobody_declared_still_gets_a_tile(self):
        build = builder(CPU)
        tiles = by_key(build.build(cpu_sample() + [
            reading("mystery", "Temperatures", "temp1", "Chip", 44.0, "temp")]))
        self.assertIn("mystery", tiles)
        self.assertEqual(tiles["mystery"].name, "mystery")

    def test_an_empty_sample_produces_tiles_but_no_values(self):
        build = builder(CPU)
        tiles = build.build([])
        self.assertEqual(len(tiles), 1)
        self.assertTrue(tiles[0].empty)


class MergedDrivesTest(unittest.TestCase):
    DISKS = (("disk:nvme0", "Samsung 990 Pro", 38.0),
             ("disk:sda", "CT1000MX500SSD1 (sda)", 51.0),
             ("disk:sdb", "ST8000DM004", 44.0))

    def _tiles(self, names=None):
        devices = [Device(key, name, order=40) for key, name, _ in self.DISKS]
        build = TileBuilder({d.key: d for d in devices})
        return by_key(build.build(
            [reading(key, "Temperatures", "temp1", "Temp1", value, "temp")
             for key, _name, value in self.DISKS], names=names))

    def test_several_drives_share_one_tile(self):
        tiles = self._tiles()
        self.assertEqual(list(tiles), ["drives"])
        self.assertEqual(len(tiles["drives"].chips), 3)

    def test_each_drive_leads_with_its_temperature_named(self):
        """From the first drive to the last; named, since a bare number says
        how hot and not which one to go and look at."""
        drives = self._tiles()["drives"]
        self.assertEqual([(m.label, m.text) for m in drives.chips],
                         [("Samsung 990 Pro", "38.0 °C"),
                          ("CT1000MX500SSD1 (sda)", "51.0 °C"),
                          ("ST8000DM004", "44.0 °C")])

    def volumes(self, *extra):
        devices = [Device(key, name, order=40) for key, name, _ in self.DISKS]
        build = TileBuilder({d.key: d for d in devices})
        return build.build(
            [reading(key, "Temperatures", "temp1", "Temp1", value, "temp")
             for key, _name, value in self.DISKS]
            + [reading(key, "Utilization", "vol:%s" % mount, label, used,
                       "memory_gb", total=total)
               for key, mount, label, used, total in extra])[0]

    def test_the_system_volume_rings_first_then_its_drive_then_the_rest(self):
        drives = self.volumes(
            ("disk:nvme0", "/mnt/a", "a", 1.0, 10.0),
            ("disk:sda", "/data", "data", 1.0, 10.0),
            ("disk:sda", "/boot", "boot", 0.1, 1.0),
            ("disk:sda", "/", "System", 5.0, 50.0),
            ("disk:sdb", "/mnt/b", "b", 7000.0, 8000.0))
        self.assertEqual([m.label for m in drives.rings],
                         ["System", "data", "a"])
        # Past three the volumes are rows, the boot loader's last of all.
        self.assertEqual([m.label for m in drives.rows], ["b", "boot"])

    def test_a_volume_ring_can_be_moved_away(self):
        drives = self.volumes(("disk:sda", "/", "System", 5.0, 50.0))
        self.assertEqual(drives.locked, ())
        config = display.place(display.TileConfig(), drives,
                               "disk:sda/vol:/", display.BELOW)
        self.assertEqual(config.rings, ())

    def test_a_renamed_drive_is_named_by_that_in_the_headline(self):
        drives = self._tiles(names={"disk:sda": "Scratch disk"})["drives"]
        self.assertEqual(drives.chips[1].label, "Scratch disk")

    def test_a_single_drive_keeps_its_own_tile(self):
        device = Device("disk:nvme0", "Samsung 990 Pro", order=40)
        build = TileBuilder({device.key: device})
        tiles = build.build([reading(device.key, "Temperatures", "Composite",
                                     "Composite", 38.0, "temp")])
        self.assertEqual([t.name for t in tiles], ["Samsung 990 Pro"])


class LevelTest(unittest.TestCase):
    def test_only_temperatures_are_graded(self):
        self.assertIsNone(display.level_for(100.0, "utilization"))
        self.assertIsNone(display.level_for(250.0, "power"))
        self.assertIsNone(display.level_for(None, "temp"))

    def test_the_thresholds(self):
        self.assertIsNone(display.level_for(79.9, "temp"))
        self.assertEqual(display.level_for(80.0, "temp"), "warn")
        self.assertEqual(display.level_for(89.9, "temp"), "warn")
        self.assertEqual(display.level_for(90.0, "temp"), "hot")

    def test_a_tile_reports_the_worst_reading_it_holds(self):
        build = builder(CPU)
        self.assertIsNone(build.build(cpu_sample(temp=40.0))[0].level)
        self.assertEqual(build.build(cpu_sample(temp=82.0))[0].level, "warn")
        self.assertEqual(build.build(cpu_sample(temp=95.0))[0].level, "hot")

    def test_heat_hidden_in_the_detail_still_shows_on_the_tile(self):
        """The tile's colour is the point: it is read without expanding it."""
        build = builder(CPU)
        hot_rail = reading("cpu", "Temperatures", "vrm", "VRM", 97.0, "temp")
        tile = build.build(cpu_sample(temp=40.0) + [hot_rail])[0]
        self.assertEqual(tile.level, "hot")


class CollapseSeriesTest(unittest.TestCase):
    def test_a_run_reports_its_average_low_and_high(self):
        build = TileBuilder()
        metrics = [build._metric(
            reading("cpu", "Clocks", "clock_%d" % i, "Core %d" % i, value,
                    "clock")) for i, value in enumerate((3000.0, 4000.0, 5000.0))]
        summary = collapse_series(metrics, "Clocks")
        self.assertEqual(summary.text, "4000 MHz")
        self.assertEqual((summary.lo, summary.hi), ("3000 MHz", "5000 MHz"))

    def test_a_run_of_blanked_readings_does_not_divide_by_zero(self):
        blank = Metric(uid="cpu/clock_0", key="clock_0", device="cpu",
                       label="Core 0", group="Clocks", kind="clock",
                       value=None, text=display.MISSING)
        summary = collapse_series([blank, blank], "Clocks")
        self.assertEqual(summary.text, display.MISSING)


if __name__ == "__main__":
    unittest.main()


class GraphUidTest(unittest.TestCase):
    def metric(self, uid, key, source=""):
        return Metric(uid=uid, key=key, device="cpu", label="x", group="",
                      kind="temp", value=1.0, text="1", source=source)

    def test_a_reading_graphs_itself(self):
        self.assertEqual(display.graph_uid(self.metric("cpu/Tctl", "Tctl")),
                         "cpu/Tctl")

    def test_a_made_up_row_has_nothing_to_graph(self):
        for key in ("!slot-temp", "!Clocks"):
            self.assertIsNone(display.graph_uid(self.metric("cpu/" + key,
                                                            key)))
        self.assertIsNone(display.graph_uid(None))

    def test_the_cpus_vcore_graphs_the_boards_reading(self):
        board = self.metric("mobo/in0", "in0")
        board = Metric(**dict(board.__dict__, kind="voltage", label="Vcore"))
        vcore = display.board_vcore([board])
        self.assertEqual(vcore.uid, "cpu/!vcore")
        self.assertEqual(display.graph_uid(vcore), "mobo/in0")


class BoardChipsTest(unittest.TestCase):
    BOARD = Device("mobo", "ASRock B850", order=60)

    def build(self, temps, fans=()):
        build = TileBuilder({self.BOARD.key: self.BOARD})
        return build, self.sample(temps, fans)

    @staticmethod
    def sample(temps, fans=()):
        out = [reading("mobo", "Temperatures", key, "NCT6799 %s" % key, value,
                       "temp") for key, value in temps]
        out += [reading("mobo", "Fans", "fan%d" % i, "Fan%d" % i, rpm, "fan")
                for i, rpm in enumerate(fans, 1)]
        return out

    TEMPS = (("SYSTIN", 40.0), ("CPUTIN", 36.0), ("AUXTIN5", -60.0),
             ("PCH", 0.0), ("TSI0", 47.6), ("SMBUS", 47.5), ("AUXTIN0", 27.0),
             ("DEAD", 127.0))

    def test_the_four_hottest_real_sensors_lead_hottest_first(self):
        """-60, 0 and 127 are headers with no diode, not the hottest parts."""
        build, sample = self.build(self.TEMPS, fans=(900.0,))
        tile = build.build(sample)[0]
        self.assertEqual(tile.rings, ())
        self.assertEqual([m.key for m in tile.chips],
                         ["TSI0", "SMBUS", "SYSTIN", "CPUTIN"])
        # Fans and the rest go below.
        self.assertIn("mobo/fan1", [m.uid for m in tile.rows])

    def test_the_choice_is_kept_from_when_the_app_started(self):
        build, sample = self.build(self.TEMPS)
        build.build(sample)
        warmer = dict(self.TEMPS, AUXTIN0=70.0)
        tile = build.build(self.sample(warmer.items()))[0]
        self.assertEqual([m.key for m in tile.chips],
                         ["TSI0", "SMBUS", "SYSTIN", "CPUTIN"])

    def test_the_chip_model_is_dropped_from_the_tile_labels(self):
        build, sample = self.build(self.TEMPS)
        tile = build.build(sample)[0]
        self.assertEqual(tile.chips[0].label, "TSI0")
        # The tree keeps the whole name.
        tree = dict(build.tree()[0].groups)["Temperatures"]
        self.assertEqual(tree[0].label, "NCT6799 SYSTIN")

    def test_two_chips_with_one_sensor_name_keep_their_models(self):
        build = TileBuilder({self.BOARD.key: self.BOARD})
        tile = build.build([
            reading("mobo", "Temperatures", "a", "NCT6799 SYSTIN", 40.0,
                    "temp"),
            reading("mobo", "Temperatures", "b", "IT8689 SYSTIN", 41.0,
                    "temp")])[0]
        self.assertEqual(sorted(m.label for m in tile.chips),
                         ["IT8689 SYSTIN", "NCT6799 SYSTIN"])

    def test_a_board_takes_up_to_fifty_chips(self):
        build, sample = self.build(self.TEMPS)
        tile = build.build(sample)[0]
        self.assertEqual(tile.limit(display.CHIP), 50)
        config = display.TileConfig()
        for key, _value in self.TEMPS:
            config = display.place(config, tile, "mobo/%s" % key,
                                   display.CHIP)
        self.assertEqual(len(config.chips), len(self.TEMPS))


class TreeRenderTest(unittest.TestCase):
    """The classic listing: every device, every group, every reading."""

    DISKS = (("disk:nvme0", "Samsung 990 Pro", 38.0),
             ("disk:sda", "CT1000MX500SSD1 (sda)", 51.0))

    def _build(self):
        devices = [CPU, RAM] + [Device(k, n, order=40) for k, n, _ in self.DISKS]
        build = builder(*devices)
        sample = cpu_sample(cores=4) + [
            reading("ram", "Utilization", "usage", "Memory Used", 12.0,
                    "memory_gb", total=32.0)]
        sample += [reading(k, "Temperatures", "temp1", "Temp1", v, "temp")
                   for k, _n, v in self.DISKS]
        return build, sample

    def test_one_model_per_device_drives_included(self):
        build, sample = self._build()
        build.build(sample)
        models = build.tree()
        self.assertEqual([m.key for m in models],
                         ["cpu", "ram", "disk:sda", "disk:nvme0"])

    def test_groups_follow_group_order_and_keep_every_reading(self):
        build, sample = self._build()
        build.build(sample)
        cpu = build.tree()[0]
        self.assertEqual([g for g, _ in cpu.groups],
                         ["Temperatures", "Utilization", "Clocks", "Powers"])
        clocks = dict(cpu.groups)["Clocks"]
        self.assertEqual([m.label for m in clocks],
                         ["Core 0", "Core 1", "Core 2", "Core 3"])

    def test_names_and_hidden_groups_are_honoured(self):
        build, sample = self._build()
        build.build(sample)
        models = build.tree(names={"cpu": "Desk"},
                            group_visible={"Clocks": False})
        self.assertEqual(models[0].name, "Desk")
        self.assertNotIn("Clocks", dict(models[0].groups))

    def test_the_range_is_carried_as_numbers(self):
        """The tree shows an idle sensor's min and max; the tile blanks them."""
        build, sample = self._build()
        build.build(sample)
        temp = dict(build.tree()[0].groups)["Temperatures"][0]
        self.assertEqual((temp.low, temp.high), (61.5, 61.5))
        self.assertEqual((temp.lo, temp.hi), (display.MISSING, display.MISSING))
        used = dict(build.tree()[1].groups)["Utilization"][0]
        self.assertEqual(used.total, 32.0)

    def test_a_stale_reading_keeps_its_range(self):
        build, sample = self._build()
        build.build(sample)
        build.build([r for r in sample if r.key != "Tctl"])
        for _ in range(display.Staleness.GRACE):
            build.build([r for r in sample if r.key != "Tctl"])
        temp = dict(build.tree()[0].groups)["Temperatures"][0]
        self.assertIsNone(temp.value)
        self.assertEqual(temp.low, 61.5)


class TileConfigTest(unittest.TestCase):
    """The user's own layout for a tile, and the edits that change it."""

    def tile(self, config=None, cores=4):
        build = builder(CPU)
        if config is not None:
            build.configs = {"cpu": config}
        return build.build(cpu_sample(cores=cores))[0]

    def test_json_round_trips_and_tolerates_rubbish(self):
        config = display.TileConfig(custom=True, rings=("cpu/usage_total",),
                                    chips=("cpu/Tctl",), below=("cpu/x",),
                                    hidden=("cpu/y",))
        self.assertEqual(display.TileConfig.from_json(config.to_json()),
                         config)
        for junk in ("", "{", "[1, 2]", '{"custom": "yes", "rings": 3}'):
            self.assertIsInstance(display.TileConfig.from_json(junk),
                                  display.TileConfig)

    def test_the_first_editors_glance_row_comes_back_as_chips(self):
        old = '{"style": "rings", "custom": true, "glance": ["cpu/Tctl"]}'
        self.assertEqual(display.TileConfig.from_json(old).chips,
                         ("cpu/Tctl",))

    def test_the_automatic_tile_lists_every_reading_for_the_editor(self):
        tile = self.tile()
        placements = {m.uid: p for p, m in tile.items}
        self.assertEqual(placements["cpu/usage_total"], display.RING)
        self.assertEqual(placements["cpu/!hotspot"], display.CHIP)
        self.assertEqual(placements["cpu/Tctl"], display.BELOW)
        self.assertEqual(placements["cpu/clock_0"], display.FOLDED)
        self.assertNotIn(display.HIDDEN, placements.values())

    def test_the_first_edit_starts_from_the_automatic_choice(self):
        tile = self.tile()
        config = display.place(display.TileConfig(), tile, "cpu/clock_0",
                               display.BELOW)
        self.assertTrue(config.custom)
        self.assertEqual(config.chips, ("cpu/!hotspot", "cpu/!Clocks",
                                        "cpu/rapl_Package"))
        self.assertEqual(config.below[-1], "cpu/clock_0")

    def test_a_custom_tile_shows_exactly_what_was_placed(self):
        config = display.TileConfig(custom=True,
                                    chips=("cpu/clock_1", "cpu/Tctl"),
                                    hidden=("cpu/rapl_Package",))
        tile = self.tile(config)
        self.assertEqual([m.uid for m in tile.rings], ["cpu/usage_total"])
        self.assertEqual([m.uid for m in tile.chips],
                         ["cpu/clock_1", "cpu/Tctl"])
        self.assertEqual(tile.rows, ())
        folded = [m.uid for _g, metrics in tile.detail for m in metrics]
        self.assertIn("cpu/clock_0", folded)
        self.assertNotIn("cpu/rapl_Package", folded)       # hidden, not folded
        self.assertEqual(dict((m.uid, p) for p, m in tile.items)
                         ["cpu/rapl_Package"], display.HIDDEN)

    def test_only_a_reading_with_a_top_can_be_a_ring(self):
        tile = self.tile()
        for uid in ("cpu/Tctl", "cpu/rapl_Package", "cpu/clock_0"):
            self.assertFalse(display.can_place(display.TileConfig(), tile,
                                               uid, display.RING))
            self.assertEqual(display.place(display.TileConfig(), tile, uid,
                                           display.RING),
                             display.TileConfig())

    def test_a_locked_ring_cannot_be_moved_or_hidden(self):
        tile = self.tile()
        for placement in (display.CHIP, display.BELOW, display.HIDDEN):
            self.assertFalse(display.can_place(display.TileConfig(), tile,
                                               "cpu/usage_total", placement))
            self.assertEqual(display.place(display.TileConfig(), tile,
                                           "cpu/usage_total", placement),
                             display.TileConfig())
        # And a saved layout that hides it anyway does not.
        hiding = display.TileConfig(custom=True, hidden=("cpu/usage_total",))
        self.assertEqual([m.uid for m in self.tile(hiding).rings],
                         ["cpu/usage_total"])

    def test_the_rings_after_a_locked_one_are_the_users(self):
        build = builder(CPU)
        sample = cpu_sample() + [reading("cpu", "Utilization", "other",
                                         "Other", 10.0, "utilization")]
        tile = build.build(sample)[0]
        config = display.place(display.TileConfig(), tile, "cpu/other",
                               display.RING)
        self.assertEqual(config.rings, ("cpu/other",))
        build.configs = {"cpu": config}
        self.assertEqual([m.uid for m in build.build(sample)[0].rings],
                         ["cpu/usage_total", "cpu/other"])

    def test_the_chip_row_is_capped(self):
        tile = self.tile(cores=12)
        config = display.TileConfig()
        for index in range(12):
            config = display.place(config, tile, "cpu/clock_%d" % index,
                                   display.CHIP)
        self.assertEqual(len(config.chips), tile.limit(display.CHIP))
        self.assertNotIn("cpu/clock_11", config.chips)

    def test_readings_move_within_their_own_list(self):
        tile = self.tile()
        config = display.move(display.TileConfig(), tile, "cpu/rapl_Package",
                              -2)
        self.assertEqual(config.chips[0], "cpu/rapl_Package")
        # Clamped at the ends rather than wrapping round.
        self.assertEqual(display.move(config, tile, "cpu/rapl_Package",
                                      -5).chips[0], "cpu/rapl_Package")

    def test_reset_is_back_to_automatic(self):
        config = display.TileConfig(custom=True, chips=("cpu/Tctl",))
        self.assertEqual(display.reset(config), display.TileConfig())

    def test_the_clock_average_can_be_placed(self):
        tile = self.tile(cores=8)
        summary = "cpu/!Clocks"
        config = display.place(display.TileConfig(), tile, summary,
                               display.BELOW)
        self.assertEqual(self.tile(config, cores=8).rows[-1].uid, summary)


class TileNamesAndColourTest(unittest.TestCase):
    """Names and a colour of the user's own, on top of any placement."""

    def tile(self, config):
        build = builder(CPU)
        build.configs = {"cpu": config}
        return build.build(cpu_sample())[0]

    def test_a_renamed_reading_is_called_that_on_the_tile(self):
        config = display.rename(display.TileConfig(), "cpu/!hotspot", "Die")
        tile = self.tile(config)
        self.assertEqual(tile.chips[0].label, "Die")
        self.assertEqual(dict(tile.original_labels)["cpu/!hotspot"],
                         "Hot Spot")
        # Naming is not placing: the tile still chooses for itself.
        self.assertFalse(config.custom)

    def test_an_empty_name_is_its_own_name_again(self):
        config = display.rename(display.TileConfig(), "cpu/Tctl", "Die")
        self.assertEqual(display.rename(config, "cpu/Tctl", "  "),
                         display.TileConfig())

    def test_names_and_colour_survive_a_placement_and_the_round_trip(self):
        config = display.rename(display.TileConfig(), "cpu/Tctl", "Die")
        config = display.recolour(config, "#A0B0C0")
        tile = self.tile(config)
        config = display.place(config, tile, "cpu/Tctl", display.CHIP)
        self.assertEqual(config.label("cpu/Tctl"), "Die")
        self.assertEqual(config.colour, "#a0b0c0")
        self.assertEqual(display.TileConfig.from_json(config.to_json()),
                         config)
        self.assertEqual(self.tile(config).colour, "#a0b0c0")

    def test_a_colour_that_is_not_one_is_ignored(self):
        self.assertEqual(display.recolour(display.TileConfig(), "red"),
                         display.TileConfig())
        self.assertEqual(display.TileConfig.from_json(
            '{"colour": "url(x)"}').colour, "")

    def test_restoring_defaults_forgets_names_and_colour_too(self):
        config = display.recolour(display.rename(
            display.TileConfig(custom=True), "cpu/Tctl", "Die"), "#112233")
        self.assertEqual(display.reset(config), display.TileConfig())

    def test_a_hot_tile_names_its_hottest_reading(self):
        build = builder(CPU)
        tile = build.build(cpu_sample(temp=93.0) + [
            reading("cpu", "Temperatures", "Tccd1", "Core (CCD1)", 85.0,
                    "temp")])[0]
        self.assertEqual(tile.hottest.value, 93.0)


class RingFractionTest(unittest.TestCase):
    def metric(self, kind, value, fraction=None, high=None):
        return display.Metric(uid="x", key="x", device="d", label="",
                              group="", kind=kind, value=value, text="",
                              fraction=fraction, high=high)

    def test_each_kind_has_a_sensible_scale(self):
        self.assertEqual(display.ring_fraction(
            self.metric("utilization", 40, fraction=0.4)), 0.4)
        # No natural top: against the peak it would sit full at every peak,
        # and 100 °C is not the top of a temperature.
        self.assertIsNone(display.ring_fraction(self.metric("temp", 55)))
        self.assertIsNone(display.ring_fraction(
            self.metric("fan", 600, high=1200)))
        self.assertIsNone(display.ring_fraction(self.metric("power", 30)))
        self.assertIsNone(display.ring_fraction(self.metric("temp", None)))
