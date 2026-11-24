"""Putting together a RAID 0 or RAID 5 whose own settings are lost.

The arrays here are cut from a real FAT32 image, made with mtools, by a
splitter written from md's own description of each layout - separately from
the helper's reading of them, so that one cannot simply agree with itself.
On real devices, with the kernel putting the array together, the same is
checked by tests/verify-disk-tools.py.
"""

import os
import random
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import MIB                                    # noqa: E402
from test_diskhelper import HelperCase                     # noqa: E402
from test_fatscan import MTOOLS, mtools                    # noqa: E402

KIB = 1024


def split(image, n, layout, chunk, offset):
    """The drives of an array holding `image`: [bytes] per drive.

    md's layouts, row by row: parity on the last drive moving left ("left")
    or on the first moving right ("right"); data in drive order skipping
    the parity ("asymmetric"), or starting just after it and wrapping round
    ("symmetric").
    """
    data_drives = n if layout == "raid0" else n - 1
    rows = -(-len(image) // (chunk * data_drives))
    image = image + b"\0" * (rows * chunk * data_drives - len(image))
    drives = [bytearray(offset + rows * chunk) for _ in range(n)]
    for row in range(rows):
        pieces = [image[(row * data_drives + k) * chunk:
                        (row * data_drives + k + 1) * chunk]
                  for k in range(data_drives)]
        if layout == "raid0":
            places = list(range(n))
        else:
            parity = (n - 1 - row % n) if layout in ("raid5_ls", "raid5_la") \
                else row % n
            if layout in ("raid5_la", "raid5_ra"):
                places = [d for d in range(n) if d != parity]
            else:
                places = [(parity + 1 + k) % n for k in range(n - 1)]
            total = 0
            for piece in pieces:
                total ^= int.from_bytes(piece, "little")
            drives[parity][offset + row * chunk:offset + (row + 1) * chunk] \
                = total.to_bytes(chunk, "little")
        for place, piece in zip(places, pieces):
            drives[place][offset + row * chunk:
                          offset + (row + 1) * chunk] = piece
    return [bytes(d) for d in drives]


def fat_with_files(folder, size=48 * MIB, seed=7):
    """A FAT32 image with many folders and files of many sizes: tables
    and folders spread over the whole of it."""
    rng = random.Random(seed)
    path = os.path.join(folder, "array.img")
    with open(path, "wb") as handle:
        handle.truncate(size)
    mtools("mformat", "-i", path, "-F", "-c", "1", "::")
    source = os.path.join(folder, "f")
    for number in range(24):
        mtools("mmd", "-i", path, "::/D%02d" % number)
        for each in range(3):
            with open(source, "wb") as handle:
                handle.write(rng.randbytes(rng.randrange(1000, 300 * KIB)))
            mtools("mcopy", "-i", path, source,
                   "::/D%02d/f%d.bin" % (number, each))
    with open(path, "rb") as handle:
        return handle.read()


@unittest.skipUnless(MTOOLS, "mtools is not installed")
class GuessTest(HelperCase):
    image = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        folder = tempfile.mkdtemp()
        try:
            cls.image = fat_with_files(folder)
        finally:
            shutil.rmtree(folder, True)

    def drives(self, layout, n, chunk, offset=0, missing=None, shuffle=()):
        """Fake drives holding the array; returns the members as a request
        names them, in the order `shuffle` gives (default: as made)."""
        parts = split(self.image, n, layout, chunk, offset)
        members = []
        for index, data in enumerate(parts):
            name = "sd%s" % "fghijk"[index]
            self.tree.add_disk(name, len(data) + MIB, diskseq=20 + index)
            with open(os.path.join(self.tree.dev, name), "r+b") as handle:
                handle.write(data)
            members.append({"device": os.path.join(self.tree.dev, name),
                            "identity": self.tree.identity(name)})
        if missing is not None:
            members[missing] = None
        order = list(shuffle) or list(range(n))
        return [members[i] for i in order], members

    def guess(self, members, level="auto"):
        return self.helper.op_raid_guess({"op": "raid_guess", "id": 3,
                                          "members": members,
                                          "level": level})

    def assert_found(self, result, layout, chunk, offset, members):
        self.assertTrue(result["sure"], result["why"])
        best = result["candidates"][0]
        self.assertEqual((best["layout"], best["chunk"], best["offset"],
                          best["kind"]), (layout, chunk, offset, "FAT32"))
        self.assertEqual(best["order"], [m["device"] if m else None
                                         for m in members])

    def test_a_raid0_is_found_whatever_order_the_drives_are_named_in(self):
        named, made = self.drives("raid0", 3, 64 * KIB, shuffle=(2, 0, 1))
        self.assert_found(self.guess(named), "raid0", 64 * KIB, 0, made)

    def test_a_raid5_its_layout_and_where_the_data_starts(self):
        named, made = self.drives("raid5_ls", 4, 16 * KIB, offset=MIB,
                                  shuffle=(3, 1, 0, 2))
        self.assert_found(self.guess(named), "raid5_ls", 16 * KIB, MIB,
                          made)

    def test_a_raid5_with_its_first_drive_missing(self):
        named, made = self.drives("raid5_ra", 3, 32 * KIB, missing=1,
                                  shuffle=(2, 1, 0))
        result = self.guess(named, level="raid5")
        self.assert_found(result, "raid5_ra", 32 * KIB, 0, made)

    def test_parity_tells_a_raid5_from_a_raid0(self):
        named, _made = self.drives("raid5_la", 3, 8 * KIB)
        best = self.guess(named)["candidates"][0]
        self.assertTrue(best["layout"].startswith("raid5"))
        # Each drive is a little larger than its part, as real ones are.
        self.assertGreaterEqual(best["size"], len(self.image))
        self.assertLess(best["size"], len(self.image) + 3 * MIB)

    def test_drives_that_are_not_one_array_are_not_called_one(self):
        named, _made = self.drives("raid0", 2, 64 * KIB)
        with open(named[1]["device"], "r+b") as handle:
            handle.write(os.urandom(len(self.image) // 2))
        result = self.guess(named)
        self.assertFalse(result["sure"])
        self.assertTrue(result["why"])

    def test_what_the_helper_will_and_will_not_take(self):
        named, _made = self.drives("raid0", 2, 64 * KIB)
        for members, said in (
                ([named[0]], "between 2"),
                ([named[0], named[0]], "named twice"),
                ([named[0], None, None], "one drive missing"),
                ([named[0], self.request("nvme0n1p2")], "system")):
            self.assertIn(said, self.refused({"op": "raid_guess", "id": 3,
                                              "members": members}))
        self.assertIn("not a RAID level", self.refused(
            {"op": "raid_guess", "id": 3, "members": named,
             "level": "raid6"}))

    def test_putting_it_together_is_read_only(self):
        named, _made = self.drives("raid5_ls", 3, 64 * KIB, offset=MIB)
        before = [self.read(m) for m in named]
        self.helper.op_raid_build({
            "op": "raid_build", "id": 4, "members": named,
            "layout": "raid5_ls", "chunk": 64 * KIB, "offset": MIB})
        calls = self.tools.of("dmsetup")
        # Where the data starts further in, each drive is seen through a
        # read-only window starting there; the array is put on those.
        for index, (argv, member) in enumerate(zip(calls[:3], named)):
            self.assertEqual(argv[:3], ["create", "kss-raid5-1-m%d" % index,
                                        "--readonly"])
            self.assertEqual(argv[-1].split()[2:], ["linear",
                                                    member["device"], "2048"])
        argv = calls[3]
        self.assertEqual(argv[:3], ["create", "kss-raid5-1", "--readonly"])
        table = argv[-1].split()
        self.assertEqual(table[2:7], ["raid", "raid5_ls", "2", "128",
                                      "nosync"])
        self.assertEqual(table[7:], ["3"] + sum(
            (["-", os.path.join(self.tree.dev, "mapper",
                                "kss-raid5-1-m%d" % i)] for i in range(3)),
            []))
        self.assertEqual([self.read(m) for m in named], before)
        # A RAID 0 is plain striping, and refuses a missing drive.
        self.helper.op_raid_build({
            "op": "raid_build", "id": 5, "members": named[:2],
            "layout": "raid0", "chunk": 4096, "offset": 0})
        self.assertIn("striped 2 8 %s 0 %s 0" % (named[0]["device"],
                                                named[1]["device"]),
                      self.tools.of("dmsetup")[-1][-1])
        self.assertIn("cannot be read with a drive missing", self.refused({
            "op": "raid_build", "id": 6, "members": [named[0], None,
                                                     named[2]],
            "layout": "raid0", "chunk": 4096, "offset": 0}))
        for bad in ({"chunk": 3000}, {"offset": 7}, {"layout": "raid6"}):
            request = {"op": "raid_build", "id": 7, "members": named,
                       "layout": "raid5_ls", "chunk": 4096, "offset": 0}
            request.update(bad)
            self.assertTrue(self.refused(request))

    def read(self, member):
        with open(member["device"], "rb") as handle:
            return handle.read()


class StripedTest(unittest.TestCase):
    """The helper's reading of an array against the splitter: every layout
    gives back exactly what was put in, also with a drive missing."""

    def test_every_layout_reads_back_whole(self):
        from disktree import load_helper
        diskhelper = load_helper()
        image = os.urandom(3 * MIB + 12345)
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        for layout in ("raid0",) + diskhelper.RAID5_LAYOUTS:
            for n, chunk, offset, missing in ((3, 4096, 0, None),
                                              (4, 65536, MIB, None),
                                              (4, 8192, 4096, 2)):
                if layout == "raid0" and missing is not None:
                    continue
                parts = split(image, n, layout, chunk, offset)
                fds = []
                for index, data in enumerate(parts):
                    path = os.path.join(folder, "m%d" % index)
                    with open(path, "wb") as handle:
                        handle.write(data)
                    fds.append(os.open(path, os.O_RDONLY))
                try:
                    if missing is not None:
                        os.close(fds[missing])
                        fds[missing] = None
                    array = diskhelper._Striped(fds, layout, chunk, offset,
                                                len(parts[0]))
                    self.assertEqual(array.read(0, len(image)), image,
                                     (layout, n, chunk, offset, missing))
                    self.assertEqual(array.read(777777, 5000),
                                     image[777777:782777])
                finally:
                    for fd in fds:
                        if fd is not None:
                            os.close(fd)


if __name__ == "__main__":
    unittest.main()
