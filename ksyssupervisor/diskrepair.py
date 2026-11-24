"""Putting a damaged recovered file back into a shape programs will open.

A file that comes back incomplete is usually not garbage: most of it is
there, and what is missing is the part that says where the rest is - a
ZIP archive's index, a PDF's table of pages, an image's last bytes. Each
kind is rebuilt from what survives, into a new file beside the original;
the original is never changed.

  Office documents and archives (ZIP inside): every part that is whole is
      found by its own header and put into a fresh archive.
  PDF: rewritten by whichever of qpdf, Ghostscript or Poppler is installed,
      all of which rebuild the page table from the pages they can find.
  JPEG: given the end marker it lost, so that viewers show what there is;
      and where even that fails, the small preview a camera stores inside
      the file is pulled out.
  PNG: cut back to its last complete part and closed properly.

Qt-free, and no root: these are the user's own files by now.
"""

from __future__ import annotations

import mmap
import os
import shutil
import struct
import subprocess
import zipfile
import zlib

from . import diskrecover

#: Parts of an archive larger than this are left out rather than unpacked
#: into memory.
BIGGEST_PART = 512 * 1024 * 1024
LOCAL = b"PK\x03\x04"
MARKS = (LOCAL, b"PK\x01\x02", b"PK\x05\x06", b"PK\x07\x08")


def repaired_name(path):
    stem, dot, extension = path.rpartition(".")
    if not dot or "/" in extension:
        return path + " (repaired)"
    return "%s (repaired).%s" % (stem, extension)


def _next_mark(data, start):
    found = [data.find(mark, start) for mark in MARKS]
    found = [place for place in found if place >= 0]
    return min(found) if found else len(data)


def zip_parts(data):
    """(name, bytes) for every part of a ZIP archive that is whole.

    Read from the headers in front of each part, which survive when the
    index at the end of the file does not. A part whose contents do not
    unpack, or do not match their checksum, is left out. Yields the name
    with None for those, so they can be counted.
    """
    place = data.find(LOCAL)
    while 0 <= place <= len(data) - 30:
        (_sig, _version, flags, method, _time, _date, crc, packed, size,
         name_length, extra_length) = struct.unpack(
            "<4sHHHHHIIIHH", data[place:place + 30])
        start = place + 30 + name_length + extra_length
        raw_name = bytes(data[place + 30:place + 30 + name_length])
        name = raw_name.decode("utf-8" if flags & 0x800 else "cp437",
                               "replace")
        following = data.find(LOCAL, place + 4)
        if start > len(data) or not name or name_length > 4096:
            place = following
            continue
        unknown = bool(flags & 8) or packed in (0xFFFFFFFF,) or (
            packed == 0 and size == 0 and method != 0)
        limit = _next_mark(data, start) if unknown else start + packed
        limit = min(limit, len(data))
        content = None
        try:
            if limit - start > BIGGEST_PART:
                raise ValueError("too large")
            if method == 0:
                content = bytes(data[start:limit])
            elif method == 8:
                unpacker = zlib.decompressobj(-15)
                content = unpacker.decompress(bytes(data[start:limit]),
                                              BIGGEST_PART)
                if not unpacker.eof:
                    content = None
                elif unknown:
                    # The checksum follows the data, with or without a
                    # mark of its own in front.
                    used = limit - start - len(unpacker.unused_data)
                    tail = bytes(data[start + used:start + used + 8])
                    if tail[:4] == b"PK\x07\x08":
                        tail = tail[4:]
                    if len(tail) >= 4:
                        crc = struct.unpack("<I", tail[:4])[0]
        except (zlib.error, ValueError, MemoryError):
            content = None
        if content is not None and not name.endswith("/") \
                and zlib.crc32(content) != crc:
            content = None
        yield name, content
        place = following


def rebuild_zip(path, target):
    """Write the whole parts of a damaged archive into a new one.
    Returns (parts kept, parts lost)."""
    kept = lost = 0
    with open(path, "rb") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            return 0, 0
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            with zipfile.ZipFile(target, "w",
                                 zipfile.ZIP_DEFLATED) as archive:
                seen = set()
                for name, content in zip_parts(data):
                    if content is None:
                        lost += 1
                        continue
                    if name in seen:
                        continue
                    seen.add(name)
                    archive.writestr(name, content)
                    kept += 1
    if not kept:
        os.unlink(target)
    return kept, lost


def _pdf_tools(which=shutil.which):
    """Commands that rewrite a PDF, each rebuilding its table of pages."""
    found = []
    if which("qpdf"):
        found.append(("qpdf", lambda a, b: ["qpdf", a, b], (0, 3)))
    if which("gs"):
        found.append(("gs", lambda a, b: [
            "gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            "-sDEVICE=pdfwrite", "-o", b, a], (0,)))
    if which("pdftocairo"):
        found.append(("pdftocairo", lambda a, b: ["pdftocairo", "-pdf", a,
                                                  b], (0,)))
    return found


def rebuild_pdf(path, target, tools=None, run=subprocess.run):
    """True when one of the installed tools produced a readable PDF."""
    for _name, argv, good in (tools if tools is not None else _pdf_tools()):
        try:
            done = run(argv(path, target), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                       timeout=600)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode in good and os.path.exists(target) \
                and os.path.getsize(target) > 0:
            return True
        try:
            os.unlink(target)
        except OSError:
            pass
    return False


def close_jpeg(path, target):
    """A JPEG that lost its end, with the end marker put back."""
    with open(path, "rb") as source, open(target, "wb") as out:
        shutil.copyfileobj(source, out)
        out.write(b"\xff\xd9")
    return True


def embedded_preview(path, target, smallest=2048):
    """The preview picture a camera stores inside a JPEG, written out.
    True when one was found."""
    with open(path, "rb") as handle:
        head = handle.read(4 * 1024 * 1024)
    start = head.find(b"\xff\xd8\xff", 2)
    while start > 0:
        end = head.find(b"\xff\xd9", start)
        if end < 0:
            return False
        if end + 2 - start >= smallest:
            with open(target, "wb") as out:
                out.write(head[start:end + 2])
            return True
        start = head.find(b"\xff\xd8\xff", start + 3)
    return False


def close_png(path, target):
    """A PNG cut back to its last complete part and ended properly.
    True when any picture data is left."""
    kept = [b"\x89PNG\r\n\x1a\n"]
    data = False
    with open(path, "rb") as handle:
        if handle.read(8) != kept[0]:
            return False
        while True:
            head = handle.read(8)
            if len(head) < 8:
                break
            length, kind = struct.unpack(">I4s", head)
            if kind == b"IEND" or length > 1 << 30:
                break
            body = handle.read(length)
            crc = handle.read(4)
            if len(body) < length or len(crc) < 4 \
                    or zlib.crc32(kind + body) != struct.unpack(">I", crc)[0]:
                break
            kept.append(head + body + crc)
            data = data or kind == b"IDAT"
    if not data:
        return False
    kept.append(struct.pack(">I", 0) + b"IEND"
                + struct.pack(">I", zlib.crc32(b"IEND")))
    with open(target, "wb") as out:
        out.write(b"".join(kept))
    return True


def repair(path):
    """Try to make a damaged file usable.

    Returns (new file or "", one sentence about what was done). The
    original stays as it is.
    """
    extension = path.rsplit(".", 1)[-1].lower() if "." in \
        os.path.basename(path) else ""
    target = repaired_name(path)
    try:
        if extension in diskrecover.ZIP_FAMILY:
            kept, lost = rebuild_zip(path, target)
            if not kept:
                return "", "No whole part of it could be found."
            return target, ("%d of its %d parts were whole and are in the "
                            "repaired copy%s." % (
                                kept, kept + lost,
                                "" if not lost else "; a document missing "
                                "parts may still not open"))
        if extension == "pdf":
            if not _pdf_tools():
                return "", ("No program that repairs PDF files is "
                            "installed (qpdf, or Ghostscript).")
            if rebuild_pdf(path, target):
                return target, ("Its table of pages was rebuilt from the "
                                "pages that could be found.")
            return "", "Too little of it is left to rebuild."
        if extension in ("jpg", "jpeg"):
            status, _detail = diskrecover.inspect_file(path)
            if status == diskrecover.DAMAGED:
                close_jpeg(path, target)
                return target, ("Its missing end was put back: viewers "
                                "now show the part that survived.")
            preview = repaired_name(path).replace("(repaired)", "(preview)")
            if embedded_preview(path, preview):
                return preview, ("The picture itself is lost, but the "
                                 "small preview stored inside it was "
                                 "saved.")
            return "", "Nothing of the picture could be got back."
        if extension == "png":
            if close_png(path, target):
                return target, ("Cut back to its last whole part: viewers "
                                "now show the upper part of the picture.")
            return "", "No whole part of the picture is left."
    except (OSError, MemoryError, ValueError) as exc:
        try:
            os.unlink(target)
        except OSError:
            pass
        return "", "It could not be repaired: %s." % exc
    return "", "Files of this kind cannot be repaired from here."


def repairable(entry):
    """Whether repair() has anything to try on a results entry."""
    extension = entry.name.rsplit(".", 1)[-1].lower() \
        if "." in entry.name else ""
    return bool(entry.file) and entry.status in (
        diskrecover.DAMAGED, diskrecover.UNREADABLE) and (
            extension in diskrecover.ZIP_FAMILY
            or extension in ("pdf", "jpg", "jpeg", "png"))


def repair_all(entries, progress=None):
    """Repair what can be; returns [(entry, new file, sentence)]."""
    todo = [e for e in entries if repairable(e)]
    done = []
    for index, entry in enumerate(todo):
        new, said = repair(entry.file)
        if new:
            entry.detail = "%s Saved as '%s'." % (said,
                                                  os.path.basename(new))
        done.append((entry, new, said))
        if progress is not None:
            progress(index + 1, len(todo))
    return done
