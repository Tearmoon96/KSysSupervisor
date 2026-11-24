"""Repairing recovered files: archives and Office documents, PDF, JPEG, PNG.

Real damaged files are made here and really repaired; the PDF test uses
whichever rewriting tool the machine has and is skipped where there is none.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_diskrecover import png                           # noqa: E402

from ksyssupervisor import diskrecover, diskrepair         # noqa: E402
from ksyssupervisor.diskrecover import Entry               # noqa: E402


class RepairCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, data):
        with open(self.path(name), "wb") as handle:
            handle.write(data)
        return self.path(name)


class ZipTest(RepairCase):
    PARTS = {"[Content_Types].xml": b"<Types/>" * 40,
             "word/document.xml": os.urandom(30000),
             "word/media/image1.png": os.urandom(8000),
             "docProps/core.xml": b"<core/>"}

    def archive(self, name="report.docx", streamed=False):
        """A document as an office program writes one. `streamed`: with the
        sizes after each part instead of in front, as some programs do."""
        target = self.path(name)

        class Unseekable(io.RawIOBase):
            def __init__(self, handle):
                self.handle = handle

            def writable(self):
                return True

            def write(self, data):
                return self.handle.write(data)

        with open(target, "wb") as handle:
            stream = Unseekable(handle) if streamed else handle
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as out:
                for part, content in self.PARTS.items():
                    out.writestr(part, content)
        return target

    def contents(self, path):
        with zipfile.ZipFile(path) as archive:
            self.assertIsNone(archive.testzip())
            return {name: archive.read(name) for name in archive.namelist()}

    def cut_index(self, path):
        with open(path, "rb") as handle:
            data = handle.read()
        with open(path, "wb") as handle:
            handle.write(data[:data.find(b"PK\x01\x02")])

    def test_a_document_that_lost_its_index_gets_every_part_back(self):
        for streamed in (False, True):
            path = self.archive(streamed=streamed)
            self.cut_index(path)
            self.assertEqual(diskrecover.inspect_file(path)[0],
                             diskrecover.DAMAGED)
            new, said = diskrepair.repair(path)
            self.assertEqual(new, self.path("report (repaired).docx"))
            self.assertEqual(self.contents(new), self.PARTS)
            self.assertIn("4 of its 4 parts", said)
            self.assertEqual(diskrecover.inspect_file(new)[0],
                             diskrecover.INTACT)
            os.unlink(new)

    def test_a_damaged_part_is_left_out_and_said_so(self):
        path = self.archive()
        with open(path, "rb") as handle:
            data = bytearray(handle.read())
        place = data.find(b"word/document.xml") + 600
        data[place:place + 64] = b"\0" * 64
        with open(path, "wb") as handle:
            handle.write(data)
        new, said = diskrepair.repair(path)
        found = self.contents(new)
        self.assertNotIn("word/document.xml", found)
        self.assertEqual(found["docProps/core.xml"], b"<core/>")
        self.assertIn("3 of its 4 parts", said)
        self.assertIn("may still not open", said)

    def test_a_file_cut_off_in_the_middle_keeps_what_came_before(self):
        path = self.archive()
        with open(path, "rb") as handle:
            data = handle.read()
        cut = data.find(b"word/media/image1.png") + 2000
        with open(path, "wb") as handle:
            handle.write(data[:cut])
        new, _said = diskrepair.repair(path)
        self.assertEqual(sorted(self.contents(new)),
                         ["[Content_Types].xml", "word/document.xml"])

    def test_nothing_whole_makes_no_file(self):
        path = self.write("empty.xlsx", b"PK\x03\x04" + os.urandom(500))
        new, said = diskrepair.repair(path)
        self.assertEqual(new, "")
        self.assertIn("No whole part", said)
        self.assertEqual(os.listdir(self.dir), ["empty.xlsx"])

    def test_the_original_is_never_changed(self):
        path = self.archive()
        self.cut_index(path)
        with open(path, "rb") as handle:
            before = handle.read()
        diskrepair.repair(path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)


class PictureTest(RepairCase):
    def test_a_png_is_cut_back_to_its_last_whole_part(self):
        path = self.path("shot.png")
        png(path, side=200)
        size = os.path.getsize(path)
        with open(path, "r+b") as handle:
            handle.truncate(size - 30)
        # One block of picture data: cut inside it, nothing is left.
        new, said = diskrepair.repair(path)
        self.assertEqual(new, "")
        self.assertIn("No whole part", said)

    def test_a_png_with_several_parts_keeps_the_whole_ones(self):
        import struct
        import zlib

        def chunk(kind, data):
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data)))
        raw = zlib.compress(b"".join(b"\0" + os.urandom(96)
                                     for _ in range(32)))
        third = len(raw) // 3
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
            ">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw[:third]) + chunk(b"IDAT", raw[third:2 * third])
            + chunk(b"IDAT", raw[2 * third:]) + chunk(b"IEND", b""))
        path = self.write("parts.png", data[:-(len(raw) // 3) - 5])
        new, said = diskrepair.repair(path)
        self.assertTrue(new.endswith("parts (repaired).png"))
        self.assertIn("upper part", said)
        with open(new, "rb") as handle:
            fixed = handle.read()
        self.assertEqual(fixed.count(b"IDAT"), 2)
        self.assertTrue(fixed.endswith(chunk(b"IEND", b"")))

    def test_a_jpeg_gets_its_end_back(self):
        path = self.write("photo.jpg",
                          b"\xff\xd8\xff\xe0" + b"\x11" * 5000)
        new, said = diskrepair.repair(path)
        self.assertTrue(new.endswith("photo (repaired).jpg"))
        with open(new, "rb") as handle:
            self.assertTrue(handle.read().endswith(b"\xff\xd9"))
        self.assertEqual(diskrecover.inspect_file(new)[0],
                         diskrecover.INTACT)
        self.assertIn("missing end", said)

    def test_the_cameras_preview_is_pulled_out_of_a_lost_picture(self):
        preview = b"\xff\xd8\xff\xdb" + b"\x22" * 4000 + b"\xff\xd9"
        path = self.write("lost.jpg", b"\0" * 12 + b"Exif" + b"\0" * 300
                          + preview + b"\x33" * 9000)
        self.assertEqual(diskrecover.inspect_file(path)[0],
                         diskrecover.UNREADABLE)
        new, said = diskrepair.repair(path)
        self.assertTrue(new.endswith("lost (preview).jpg"))
        with open(new, "rb") as handle:
            self.assertEqual(handle.read(), preview)
        self.assertIn("preview", said)

    def test_a_picture_with_nothing_left_says_so(self):
        path = self.write("gone.jpg", os.urandom(4000).replace(
            b"\xff\xd8\xff", b"abc"))
        self.assertEqual(diskrepair.repair(path)[0], "")


class PdfTest(RepairCase):
    def test_each_installed_tool_is_tried_until_one_works(self):
        calls = []

        class Done:
            def __init__(self, code):
                self.returncode = code

        def run(argv, **_options):
            calls.append(argv[0])
            if argv[0] == "second":
                with open(argv[-1], "wb") as handle:
                    handle.write(b"%PDF-1.4 rebuilt")
                return Done(0)
            return Done(1)

        tools = [("first", lambda a, b: ["first", a, b], (0,)),
                 ("second", lambda a, b: ["second", a, b], (0,)),
                 ("third", lambda a, b: ["third", a, b], (0,))]
        source = self.write("a.pdf", b"%PDF-1.4 broken")
        target = self.path("out.pdf")
        self.assertTrue(diskrepair.rebuild_pdf(source, target, tools, run))
        self.assertEqual(calls, ["first", "second"])
        self.assertFalse(diskrepair.rebuild_pdf(source, self.path("no.pdf"),
                                                tools[:1], run))
        self.assertFalse(os.path.exists(self.path("no.pdf")))

    def test_no_tool_installed_names_what_would_do_it(self):
        from unittest import mock
        path = self.write("a.pdf", b"%PDF-1.4 broken")
        with mock.patch.object(diskrepair, "_pdf_tools", lambda: []):
            new, said = diskrepair.repair(path)
        self.assertEqual(new, "")
        self.assertIn("qpdf", said)

    @unittest.skipUnless(shutil.which("gs") or shutil.which("qpdf"),
                         "no PDF rewriting tool on this machine")
    def test_a_real_pdf_without_its_page_table_is_rebuilt(self):
        body = (b"%PDF-1.4\n"
                b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
                b">>endobj\n")
        path = self.write("cut.pdf", body)       # no table, no trailer
        self.assertEqual(diskrecover.inspect_file(path)[0],
                         diskrecover.DAMAGED)
        new, said = diskrepair.repair(path)
        self.assertTrue(new, said)
        self.assertEqual(diskrecover.inspect_file(new)[0],
                         diskrecover.INTACT)


class BatchTest(RepairCase):
    def test_only_what_is_damaged_and_of_a_known_kind_is_tried(self):
        good = self.path("ok.png")
        png(good)
        cut = self.write("cut.jpg", b"\xff\xd8\xff\xe0" + b"\x11" * 900)
        entries = [Entry("ok.png", file=good),
                   Entry("cut.jpg", file=cut),
                   Entry("data.bin", file=self.write("data.bin", b"x" * 99)),
                   Entry("not-recovered.jpg")]
        diskrecover.inspect_all(entries)
        self.assertEqual([diskrepair.repairable(e) for e in entries],
                         [False, True, False, False])
        seen = []
        done = diskrepair.repair_all(entries,
                                     lambda a, b: seen.append((a, b)))
        self.assertEqual(len(done), 1)
        self.assertEqual(seen, [(1, 1)])
        self.assertIn("Saved as 'cut (repaired).jpg'", entries[1].detail)

    def test_names_without_an_extension_do_not_confuse_it(self):
        self.assertEqual(diskrepair.repaired_name("/a/b.c/file"),
                         "/a/b.c/file (repaired)")
        self.assertEqual(diskrepair.repaired_name("/a/x.tar.gz"),
                         "/a/x.tar (repaired).gz")


if __name__ == "__main__":
    unittest.main()
