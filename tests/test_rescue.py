"""The recovery stick: putting its system together, writing it, and the
dialog that asks for it.

What it does on a real stick and in a real computer is pinned down by
tests/verify-disk-tools.py ("stick") and by starting the result in a
virtual machine; here, the parts one by one, against fixture trees.
"""

import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from disktree import MIB, load_helper                      # noqa: E402
from test_diskhelper import HelperCase                     # noqa: E402
from test_disks import desktop, parse, stick               # noqa: E402
from test_diskwindow import DiskWindowCase                 # noqa: E402

from ksyssupervisor import (diskcopy, disks, rescuebuild,  # noqa: E402
                            rescueinit)


def read_cpio(data):
    """{path: (mode, contents or link target)} and the order, from a newc
    archive - the kernel's reading of it, in short."""
    found, order, offset = {}, [], 0
    while True:
        header = data[offset:offset + 110]
        assert header[:6] == b"070701", header[:6]
        fields = [int(header[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        mode, size, namesize = fields[1], fields[6], fields[11]
        offset += 110
        name = data[offset:offset + namesize - 1].decode()
        offset += namesize
        offset += -offset % 4
        body = data[offset:offset + size]
        offset += size
        offset += -offset % 4
        if name == "TRAILER!!!":
            return found, order
        found["/" + name] = (mode, body)
        order.append("/" + name)


class Tree:
    """A small make-believe system to build from."""

    def __init__(self):
        self.root = tempfile.mkdtemp()

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def file(self, relative, data=b"x", mode=0o644):
        path = self.path(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode())
        os.chmod(path, mode)
        return path

    def link(self, relative, target):
        path = self.path(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return path


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.tree = Tree()
        self.addCleanup(__import__("shutil").rmtree, self.tree.root, True)

    def test_every_symlink_on_the_way_is_listed(self):
        tree = self.tree
        tree.file("usr/lib64/libfoo.so.1.2")
        tree.link("usr/lib64/libfoo.so.1", "libfoo.so.1.2")
        tree.link("lib64", "usr/lib64")
        links, real = rescuebuild.resolve_chain(tree.path("lib64",
                                                          "libfoo.so.1"))
        self.assertEqual(real, tree.path("usr/lib64/libfoo.so.1.2"))
        self.assertEqual(links, [(tree.path("lib64"), "usr/lib64"),
                                 (tree.path("usr/lib64/libfoo.so.1"),
                                  "libfoo.so.1.2")])

    def test_a_loop_is_stopped(self):
        self.tree.link("a", "b")
        self.tree.link("b", "a")
        with self.assertRaises(rescuebuild.BuildError):
            rescuebuild.resolve_chain(self.tree.path("a"))

    def test_the_image_keeps_both_names_and_moves_what_it_is_told(self):
        tree = self.tree
        tree.file("app/pkg/mod.py", "print(1)")
        tree.link("app/pkg/alias.py", "mod.py")
        image = rescuebuild.Image([(tree.path("app"), "/opt/kss")])
        image.add_tree(tree.path("app"))
        self.assertEqual(image.entries["/opt/kss/pkg/mod.py"].kind, "file")
        self.assertEqual(image.entries["/opt/kss/pkg/alias.py"].target,
                         "mod.py")
        self.assertEqual(image.entries["/opt/kss"].kind, "dir")
        self.assertEqual(image.entries["/opt"].kind, "dir")


class ArchiveTest(unittest.TestCase):
    def test_the_archive_reads_back_with_parents_first(self):
        image = rescuebuild.Image()
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"hello\n" * 1000)
        self.addCleanup(os.unlink, handle.name)
        image._put("/usr/bin/tool", rescuebuild.Entry(
            "file", 0o755, source=handle.name))
        image.add_data("/etc/x.conf", "a=1\n")
        image.add_link("/bin", "usr/bin")
        image.add_node("/dev/console", 5, 1)
        out = io.BytesIO()
        rescuebuild.write_cpio(image, out)
        data = out.getvalue()
        self.assertEqual(len(data) % 512, 0)
        found, order = read_cpio(data)
        self.assertEqual(found["/usr/bin/tool"][1], b"hello\n" * 1000)
        self.assertEqual(stat.S_IMODE(found["/usr/bin/tool"][0]), 0o755)
        self.assertEqual(found["/bin"][1], b"usr/bin")
        self.assertTrue(stat.S_ISLNK(found["/bin"][0]))
        self.assertTrue(stat.S_ISCHR(found["/dev/console"][0]))
        self.assertTrue(stat.S_ISDIR(found["/usr/bin"][0]))
        for path in order:
            parent = os.path.dirname(path)
            if parent != "/":
                self.assertLess(order.index(parent), order.index(path), path)

    def test_the_compression_is_one_the_kernel_reads(self):
        kind, engine = rescuebuild.compressor()
        packed = engine.compress(b"abc" * 1000) + engine.flush()
        if kind == "zstd":
            from compression import zstd
            self.assertEqual(zstd.decompress(packed), b"abc" * 1000)
            self.assertEqual(packed[:4], b"\x28\xb5\x2f\xfd")
        else:
            import lzma
            self.assertEqual(lzma.decompress(packed), b"abc" * 1000)


class ProgramTest(unittest.TestCase):
    def setUp(self):
        self.tree = Tree()
        self.addCleanup(__import__("shutil").rmtree, self.tree.root, True)

    def test_libraries_and_what_they_load_at_run_time_come_along(self):
        tree = self.tree
        tool = tree.file("bin/tool", b"\x7fELF....", 0o755)
        sdl2 = tree.file("lib/libSDL2-2.0.so.0", b"\x7fELF")
        sdl3 = tree.file("lib/libSDL3.so.0", b"\x7fELF")
        deps = {tool: [sdl2], sdl2: [], sdl3: []}
        image = rescuebuild.Image()
        image.add_program(tool, ldd=lambda path: deps[path])
        self.assertIn(sdl2, image.entries)
        self.assertIn(sdl3, image.entries)

    def test_a_script_brings_its_interpreter(self):
        tree = self.tree
        shell = tree.file("bin/sh", b"\x7fELF", 0o755)
        script = tree.file("bin/run", "#!%s -e\necho\n" % shell, 0o755)
        image = rescuebuild.Image()
        image.add_program(script, ldd=lambda path: [])
        self.assertIn(shell, image.entries)

    def test_a_library_that_is_not_installed_stops_the_build(self):
        with mock.patch.object(rescuebuild.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=(
                "\tlibgone.so.1 => not found\n"
                "\tlibc.so.6 => /lib64/libc.so.6 (0x0001)\n"
                "\t/lib64/ld-linux-x86-64.so.2 (0x0002)\n"))
            with self.assertRaises(rescuebuild.BuildError) as caught:
                rescuebuild.library_deps("/bin/x")
        self.assertIn("libgone.so.1", str(caught.exception))
        with mock.patch.object(rescuebuild.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=(
                "\tlinux-vdso.so.1 (0x0003)\n"
                "\tlibc.so.6 => /lib64/libc.so.6 (0x0001)\n"
                "\t/lib64/ld-linux-x86-64.so.2 (0x0002)\n"))
            self.assertEqual(rescuebuild.library_deps("/bin/x"),
                             ["/lib64/libc.so.6",
                              "/lib64/ld-linux-x86-64.so.2"])


class ModulesTest(unittest.TestCase):
    def test_storage_input_and_filesystems_go_graphics_drivers_stay(self):
        tree = Tree()
        self.addCleanup(__import__("shutil").rmtree, tree.root, True)
        base = "lib/modules/7.0"
        for name in ("kernel/drivers/nvme/host/nvme.ko.xz",
                     "kernel/drivers/nvme/host/nvme-core.ko.xz",
                     "kernel/fs/vfat/vfat.ko.xz",
                     "kernel/fs/fat/fat.ko.xz",
                     "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko.xz",
                     "kernel/drivers/gpu/drm/tiny/simpledrm.ko.xz",
                     "kernel/drivers/gpu/drm/drm.ko.xz",
                     "kernel/lib/crc32.ko.xz"):
            tree.file(os.path.join(base, name))
        tree.file(os.path.join(base, "modules.dep"),
                  "kernel/drivers/nvme/host/nvme.ko.xz: "
                  "kernel/drivers/nvme/host/nvme-core.ko.xz\n"
                  "kernel/fs/vfat/vfat.ko.xz: kernel/fs/fat/fat.ko.xz\n"
                  "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko.xz: "
                  "kernel/drivers/gpu/drm/drm.ko.xz\n"
                  "kernel/drivers/gpu/drm/tiny/simpledrm.ko.xz: "
                  "kernel/drivers/gpu/drm/drm.ko.xz\n")
        tree.file(os.path.join(base, "modules.alias"), "")
        image = rescuebuild.Image()
        rescuebuild.add_modules(image, tree.path(base))
        names = {os.path.basename(p) for p in image.entries}
        self.assertTrue({"nvme.ko.xz", "nvme-core.ko.xz", "vfat.ko.xz",
                         "fat.ko.xz", "simpledrm.ko.xz", "drm.ko.xz",
                         "crc32.ko.xz", "modules.dep",
                         "modules.alias"} <= names)
        self.assertNotIn("amdgpu.ko.xz", names)


class SettingsTest(unittest.TestCase):
    def test_the_keyboard_comes_from_localectl_then_the_files(self):
        run = mock.Mock(return_value=mock.Mock(stdout=(
            "   System Locale: LANG=it_IT.UTF-8\n"
            "       X11 Layout: it,us\n      X11 Variant: winkeys\n")))
        self.assertEqual(rescuebuild.keyboard_layout(run, ()),
                         ("it", "winkeys"))
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write('XKBMODEL="pc105"\nXKBLAYOUT="de"\nXKBVARIANT=""\n')
        self.addCleanup(os.unlink, handle.name)
        failing = mock.Mock(side_effect=OSError)
        self.assertEqual(rescuebuild.keyboard_layout(failing,
                                                     (handle.name,)),
                         ("de", ""))
        self.assertEqual(rescuebuild.keyboard_layout(failing, ()),
                         ("us", ""))

    def test_icons_are_the_names_the_code_uses_that_the_theme_has(self):
        tree = Tree()
        self.addCleanup(__import__("shutil").rmtree, tree.root, True)
        tree.file("pkg/a.py", 'x = ("drive-harddisk", "not-an-icon")\n')
        tree.file("icons/breeze/index.theme", "[Icon Theme]\n")
        tree.file("icons/breeze/devices/22/drive-harddisk.svg", "<svg/>")
        tree.file("icons/breeze/apps/22/firefox.svg", "<svg/>")
        names = rescuebuild.icon_names(tree.path("pkg"))
        found = rescuebuild.icon_files(names, (tree.path("icons"),))
        self.assertEqual(sorted(os.path.basename(p) for p in found),
                         ["drive-harddisk.svg", "index.theme"])


class ToolsTest(unittest.TestCase):
    """Every tool the helper may run is on the stick, or is left off on
    purpose."""

    #: What the helper names that the stick does without.
    NOT_ON_STICK = {
        "systemctl",       # no systemd on the stick: TRIM timers are moot
        "mkfs.ext2", "mkfs.ext3",  # mke2fs and mkfs.ext4 do the work
        "grub2-mkimage", "grub-mkimage",  # only making a stick needs them
        "mkfs.ntfs",       # a link to mkntfs, taken along when present
    }

    def test_the_helpers_tools_are_all_on_the_stick(self):
        from disktree import HELPER
        module = load_helper()
        with open(HELPER) as handle:
            text = handle.read()
        named = set()
        # Calls naming their tools outright; the others name them from the
        # tables, which are read below.
        literal = r'(?:need|find_tool)\(((?:\s*"[^"]+"\s*,?)+)\)'
        for call in re.findall(literal, text):
            named.update(re.findall(r'"([^"]+)"', call))
        for spec in module.FILESYSTEMS.values():
            named.update(spec["mkfs"])
        for spec in module.RESIZE.values():
            named.add(spec["tool"])
        for tool, _argv in module.SMART_TOOLS.values():
            named.add(tool)
        for tool, _argv in module.LABEL_TOOLS.values():
            named.add(tool)
        for spec in module.CHECK_TOOLS.values():
            if isinstance(spec, dict):
                named.update(spec["tool"])
        self.assertEqual(
            named - set(rescuebuild.RESCUE_TOOLS) - self.NOT_ON_STICK, set())


class BuildTest(unittest.TestCase):
    """The whole build against a make-believe system: what ends up where,
    and the manifest the helper checks the files by."""

    def setUp(self):
        import shutil
        self.tree = tree = Tree()
        self.addCleanup(shutil.rmtree, tree.root, True)
        self.release = "7.0.0-test"
        modules = "lib/modules/%s" % self.release
        tree.file(modules + "/vmlinuz", b"KERNEL" * 100)
        tree.file(modules + "/kernel/fs/vfat/vfat.ko.xz")
        tree.file(modules + "/modules.dep", "kernel/fs/vfat/vfat.ko.xz:\n")
        self.python = tree.file("usr/bin/python3.99", b"\x7fELF", 0o755)
        tree.file("usr/lib64/python3.99/os.py", "")
        tree.file("usr/lib64/python3.99/test/test_x.py", "")
        tree.file("usr/lib64/python3.99/lib-dynload/_ssl.so", b"\x7fELF")
        tree.file("usr/lib64/python3.99/lib-dynload/_tkinter.so", b"\x7fELF")
        tree.file("site/PyQt6/__init__.py", "")
        tree.file("site/PyQt6/QtCore.abi3.so", b"\x7fELF")
        tree.file("site/PyQt6/QtQuick.abi3.so", b"\x7fELF")
        tree.file("site/PyQt6/QtCore.pyi", "")
        tree.file("site/psutil/__init__.py", "")
        tree.file("app/ksyssupervisor/__init__.py", "")
        tree.file("app/ksyssupervisor/disks.py", "")
        tree.file("app/KSysSupervisor.py", "")
        tree.file("app/helpers/ksyssupervisor-diskhelper", "#!/usr/bin/env "
                  "python3\n", 0o755)
        for name in ("sfdisk", "mkfs.fat", "bash", "udevadm"):
            tree.file("usr/sbin/" + name, b"\x7fELF", 0o755)
        tree.file("usr/lib/udev/rules.d/60-input.rules", "")
        tree.file("plugins/platforms/libqlinuxfb.so", b"\x7fELF")
        tree.file("plugins/generic/libqevdevmouseplugin.so", b"\x7fELF")
        tree.file("fonts/NotoSans-Regular.ttf", b"font")
        self.host = rescuebuild.Host(
            kernel_release=self.release, modules_root=tree.path(
                "lib/modules"), boot_dir=tree.path("boot"),
            python=self.python, stdlib=tree.path("usr/lib64/python3.99"),
            package=tree.path("app/ksyssupervisor"), appdir="",
            tool_dirs=(tree.path("usr/sbin"),),
            udev_dirs=(tree.path("usr/lib/udev"),),
            udevd=(tree.path("usr/sbin/udevadm"),),
            icon_roots=(), xkb=tree.path("none"),
            libinput=tree.path("none"), locales=(), terminfo=(),
            cursor_config=tree.path("none"))
        sites = {"PyQt6": tree.path("site/PyQt6"),
                 "psutil": tree.path("site/psutil")}

        def find_spec(name):
            return mock.Mock(submodule_search_locations=[sites[name]])

        patches = (
            mock.patch("importlib.util.find_spec", find_spec),
            mock.patch.object(rescuebuild, "qt_plugin_dir",
                              lambda: tree.path("plugins")),
            mock.patch.object(rescuebuild, "ETC_FILES", ()))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def build(self, **settings):
        out = os.path.join(self.tree.root, "out")
        result = rescuebuild.build(
            out, host=self.host, settings=rescuebuild.Settings(**settings),
            ldd=lambda path: [],
            fonts=("Noto Sans", [self.tree.path(
                "fonts/NotoSans-Regular.ttf")]))
        with open(result.initrd, "rb") as handle:
            packed = handle.read()
        if result.compression == "zstd":
            from compression import zstd
            data = zstd.decompress(packed)
        else:
            import lzma
            data = lzma.decompress(packed)
        return result, read_cpio(data)[0]

    def test_the_system_has_what_it_needs_and_not_more(self):
        result, found = self.build(keyboard="it")
        tree = self.tree
        place = lambda p: p.replace(tree.path("app"), "/opt/ksyssupervisor")
        self.assertIn(self.python, found)
        self.assertIn(tree.path("usr/lib64/python3.99/os.py"), found)
        self.assertNotIn(tree.path("usr/lib64/python3.99/test/test_x.py"),
                         found)
        self.assertNotIn(tree.path(
            "usr/lib64/python3.99/lib-dynload/_tkinter.so"), found)
        self.assertIn(tree.path("site/PyQt6/QtCore.abi3.so"), found)
        self.assertNotIn(tree.path("site/PyQt6/QtQuick.abi3.so"), found)
        self.assertNotIn(tree.path("site/PyQt6/QtCore.pyi"), found)
        self.assertIn("/opt/ksyssupervisor/ksyssupervisor/disks.py", found)
        self.assertIn(place(tree.path(
            "app/helpers/ksyssupervisor-diskhelper")), found)
        self.assertIn(tree.path("usr/sbin/sfdisk"), found)
        self.assertIn(tree.path("plugins/platforms/libqlinuxfb.so"), found)
        self.assertIn(tree.path("lib/modules/%s/kernel/fs/vfat/vfat.ko.xz"
                                % self.release), found)
        self.assertIn("mkfs.exfat", result.missing)
        settings = json.loads(found[rescuebuild.MARKER][1])
        self.assertEqual(settings["keyboard"], "it")
        self.assertEqual(settings["font"], "Noto Sans")
        self.assertEqual(settings["python"], self.python)
        self.assertIn(b"<family>Noto Sans</family>",
                      found["/etc/fonts/fonts.conf"][1])
        init = found["/init"][1].decode()
        self.assertTrue(init.startswith("#!%s -I\n" % self.python))
        self.assertIn("rescueinit.main()", init)
        self.assertEqual(stat.S_IMODE(found["/init"][0]), 0o755)
        self.assertTrue(stat.S_ISCHR(found["/dev/console"][0]))

    def test_mice_go_to_the_handler_that_keeps_every_movement(self):
        # Qt's libinput handler rounds each movement to whole pixels, so a
        # slow hand moved nothing; the evdev handler adds up every count.
        _result, found = self.build()
        settings = json.loads(found[rescuebuild.MARKER][1])
        self.assertEqual(settings["pointer"], "evdev")
        self.assertIn(self.tree.path(
            "plugins/generic/libqevdevmouseplugin.so"), found)
        rules = found[rescuebuild.POINTER_RULES_FILE][1].decode()
        for line in rules.splitlines()[1:]:
            self.assertIn('ENV{LIBINPUT_IGNORE_DEVICE}="1"', line)
            # A keyboard that is also a pointer stays with libinput, or
            # its keys would be lost.
            self.assertIn('ENV{ID_INPUT_KEYBOARD}!="1"', line)
        self.assertEqual(rescueinit.app_env(settings, False)[
            "QT_QPA_GENERIC_PLUGINS"], "evdevmouse")
        # Without the plugin libinput keeps everything: a pointer that
        # sticks beats none.
        os.remove(self.tree.path("plugins/generic/libqevdevmouseplugin.so"))
        _result, found = self.build()
        settings = json.loads(found[rescuebuild.MARKER][1])
        self.assertEqual(settings["pointer"], "libinput")
        self.assertNotIn(rescuebuild.POINTER_RULES_FILE, found)
        self.assertNotIn("QT_QPA_GENERIC_PLUGINS",
                         rescueinit.app_env(settings, False))

    def test_the_monospace_font_and_the_arrow_go_along(self):
        self.tree.file("fonts/NotoSansMono-Regular.ttf", b"font")
        arrow = xcursor({24: (32, 4)})
        self.tree.file("icons/breeze_cursors/cursors/default", arrow)
        os.symlink("default", self.tree.path(
            "icons/breeze_cursors/cursors/left_ptr"))
        self.host.icon_roots = (self.tree.path("icons"),)
        out = os.path.join(self.tree.root, "out")
        result = rescuebuild.build(
            out, host=self.host, settings=rescuebuild.Settings(),
            ldd=lambda path: [],
            fonts=("Noto Sans", [self.tree.path(
                "fonts/NotoSans-Regular.ttf")], "Noto Sans Mono"))
        from compression import zstd
        with open(result.initrd, "rb") as handle:
            found = read_cpio(zstd.decompress(handle.read()))[0]
        conf = found["/etc/fonts/fonts.conf"][1].decode()
        self.assertIn("<family>monospace</family><prefer><family>Noto Sans "
                      "Mono</family>", conf)
        settings = json.loads(found[rescuebuild.MARKER][1])
        self.assertEqual(settings["mono"], "Noto Sans Mono")
        cursor = self.tree.path("icons/breeze_cursors/cursors/default")
        self.assertEqual(settings["cursor"], cursor)
        self.assertEqual(found[cursor][1], arrow)

    def test_the_manifest_describes_the_two_files_exactly(self):
        result, _found = self.build()
        with open(result.manifest) as handle:
            manifest = json.load(handle)
        for name in ("vmlinuz", "initrd.img"):
            path = os.path.join(result.folder, name)
            with open(path, "rb") as handle:
                data = handle.read()
            self.assertEqual(manifest["files"][name]["size"], len(data))
            self.assertEqual(manifest["files"][name]["sha256"],
                             hashlib.sha256(data).hexdigest())
        self.assertFalse(os.path.exists(result.initrd + ".partial"))

    def test_a_missing_kernel_or_screen_plugin_says_so(self):
        os.remove(self.tree.path("lib/modules/%s/vmlinuz" % self.release))
        with self.assertRaises(rescuebuild.BuildError) as caught:
            self.build()
        self.assertIn("running kernel", str(caught.exception))
        self.tree.file("lib/modules/%s/vmlinuz" % self.release, b"K")
        os.remove(self.tree.path("plugins/platforms/libqlinuxfb.so"))
        with self.assertRaises(rescuebuild.BuildError) as caught:
            self.build()
        self.assertIn("libqlinuxfb.so", str(caught.exception))


def xcursor(sizes):
    """An Xcursor file: {nominal size: (pixels square, hot spot)}."""
    header = b"Xcur" + (16).to_bytes(4, "little") + (0x10000).to_bytes(
        4, "little") + len(sizes).to_bytes(4, "little")
    toc, chunks = b"", b""
    where = 16 + 12 * len(sizes)
    for nominal, (side, hot) in sorted(sizes.items()):
        chunk = b"".join(n.to_bytes(4, "little") for n in (
            36, 0xfffd0002, nominal, 1, side, side, hot, hot, 0))
        chunk += bytes([nominal % 256, 0, 0, 255]) * side * side
        toc += b"".join(n.to_bytes(4, "little") for n in (
            0xfffd0002, nominal, where + len(chunks)))
        chunks += chunk
    return header + toc + chunks


class StickLookTest(unittest.TestCase):
    """The one pointer the stick shows, and its fixed-width text."""

    def test_the_arrow_nearest_the_screen_size_is_taken(self):
        from ksyssupervisor.rescuewindow import xcursor_image
        data = xcursor({24: (32, 4), 36: (48, 6), 48: (64, 8)})
        self.assertEqual(xcursor_image(data, 24)[:4], (32, 32, 4, 4))
        self.assertEqual(xcursor_image(data, 36)[:4], (48, 48, 6, 6))
        # 30 is as near 24 as 36: the larger one, on a large screen.
        self.assertEqual(xcursor_image(data, 30)[:4], (48, 48, 6, 6))
        self.assertEqual(xcursor_image(data, 200)[:4], (64, 64, 8, 8))
        self.assertEqual(len(xcursor_image(data, 24)[4]), 32 * 32 * 4)
        self.assertIsNone(xcursor_image(b"not a cursor", 24))
        self.assertIsNone(xcursor_image(data[:200], 24))

    def test_the_desktop_theme_is_looked_for_first(self):
        root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, root, True)
        for theme in ("breeze_cursors", "Mine"):
            folder = os.path.join(root, theme, "cursors")
            os.makedirs(folder)
            with open(os.path.join(folder, "left_ptr"), "wb") as handle:
                handle.write(xcursor({24: (32, 4)}))
        config = os.path.join(root, "kcminputrc")
        with open(config, "w") as handle:
            handle.write("[Keyboard]\ncursorTheme=Wrong\n"
                         "[Mouse]\ncursorTheme=Mine\n")
        self.assertEqual(rescuebuild.cursor_file((root,), config),
                         os.path.join(root, "Mine", "cursors", "left_ptr"))
        self.assertEqual(rescuebuild.cursor_file((root,), "/nonexistent"),
                         os.path.join(root, "breeze_cursors", "cursors",
                                      "left_ptr"))
        self.assertIsNone(rescuebuild.cursor_file(("/nonexistent",), config))

    def test_without_a_desktop_fixed_text_asks_for_monospace(self):
        # On the stick Qt has no fixed-width font to give - one with no
        # family, which draws every character as a box.
        from PyQt6.QtGui import QFont
        from ksyssupervisor import widgets
        with mock.patch.object(widgets.QFontDatabase, "systemFont",
                               return_value=QFont("")):
            font = widgets.fixed_font()
        self.assertEqual(font.family(), "monospace")
        self.assertEqual(font.styleHint(), QFont.StyleHint.TypeWriter)
        with mock.patch.object(widgets.QFontDatabase, "systemFont",
                               return_value=QFont("Hack")):
            self.assertEqual(widgets.fixed_font().family(), "Hack")


class InitTest(unittest.TestCase):
    def test_the_boot_line_and_the_environment(self):
        with mock.patch("builtins.open", mock.mock_open(
                read_data="BOOT_IMAGE=/kss/vmlinuz kss.stick=ABCD-1234 "
                          "quiet kss.debug kss.shell=ttyS1\n")):
            self.assertEqual(rescueinit.cmdline(), {
                "stick": "ABCD-1234", "debug": "", "shell": "ttyS1"})
        env = rescueinit.app_env({"keyboard": "it", "variant": ""}, True,
                                 debug=True)
        self.assertEqual(env["XKB_DEFAULT_LAYOUT"], "it")
        self.assertEqual(env["QT_QPA_PLATFORM"], "linuxfb")
        self.assertEqual(env["QT_QPA_FB_DRM"],
                         "0" if os.path.exists("/dev/fb0") else "1")
        self.assertTrue(env["XDG_STATE_HOME"].startswith(
            rescueinit.STICK_MOUNT))
        self.assertEqual(env["QT_DEBUG_PLUGINS"], "1")
        self.assertNotIn("XDG_STATE_HOME", rescueinit.app_env({}, False))

    def test_the_stick_mount_is_a_system_mount_on_both_sides(self):
        self.assertTrue(disks.is_system_mount(rescueinit.STICK_MOUNT))
        self.assertIn(rescueinit.STICK_MOUNT, load_helper().SYSTEM_MOUNTS)
        self.assertEqual(rescueinit.STATE_DIR, load_helper().STATE_DIR)


class StickWriteTest(HelperCase):
    """The helper's half, on the fixture's 8 GiB stick sdd."""

    def setUp(self):
        super().setUp()
        self.stage = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.stage, True)
        self.files = {}
        for name, data in (("vmlinuz", b"K" * 5000),
                           ("initrd.img", b"I" * 300000)):
            path = os.path.join(self.stage, name)
            with open(path, "wb") as handle:
                handle.write(data)
            self.files[name] = {"size": len(data), "sha256":
                                hashlib.sha256(data).hexdigest()}
        efi = os.path.join(self.root, "efi")
        fedora = os.path.join(efi, "fedora")
        os.makedirs(fedora)
        for name in ("shimx64.efi", "grubx64.efi", "mmx64.efi",
                     "fbx64.efi"):
            with open(os.path.join(fedora, name), "wb") as handle:
                handle.write(name.encode())
        self.module.EFI_ROOTS = (efi,)
        self.module.PACKAGED_SHIMS = ()
        self.module.GRUB_MODULE_DIRS = ()
        # What blkid reads from the new filesystem.
        self.tools.lie["sdd1"] = {"TYPE": "vfat", "LABEL": "KSSRESCUE",
                                  "UUID": "ABCD-1234"}
        self.mounts = []
        real = self.module.Helper.mounted_somewhere

        def remember(helper, device, fstype, options=()):
            manager = real(helper, device, fstype, options)

            class Keep:
                def __enter__(inner):
                    point = manager.__enter__()
                    # A real mount shows the stick's files again; the fake
                    # one is an empty folder, so they are put there.
                    if self.mounts and getattr(self, "copy", None):
                        __import__("shutil").copytree(self.copy, point,
                                                      dirs_exist_ok=True)
                    self.mounts.append(point)
                    self.kept = point
                    return point

                def __exit__(inner, *exc):
                    # The point is removed on the way out; keep its files.
                    keep = os.path.join(self.root, "stick-%d"
                                        % len(self.mounts))
                    __import__("shutil").copytree(self.kept, keep)
                    self.copy = keep
                    return manager.__exit__(*exc)
            return Keep()
        patch = mock.patch.object(self.module.Helper, "mounted_somewhere",
                                  remember)
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, **fields):
        request = self.request("sdd", op="rescue_write", stage=self.stage,
                               files=self.files, verify=True)
        for key in ("fstype", "label", "erase", "table"):
            request.pop(key)
        request.update(fields)
        return request

    def test_the_stick_gets_one_fat32_partition_the_loader_and_the_system(
            self):
        result = self.helper.op_rescue_write(self.write())
        self.assertEqual(result["uuid"], "ABCD-1234")
        self.assertTrue(result["secure_boot"])
        scripts = [stdin for name, _a, stdin in self.tools.calls
                   if name == "sfdisk" and stdin]
        self.assertIn("type=c, bootable", scripts[-1])
        mkfs = self.tools.of("mkfs.fat")[-1]
        self.assertIn("KSSRESCUE", mkfs)
        self.assertIn("32", mkfs)
        stick = self.copy
        listed = sorted(os.listdir(os.path.join(stick, "EFI", "BOOT")))
        self.assertEqual(listed, ["BOOTX64.EFI", "grub.cfg", "grubx64.efi",
                                  "mmx64.efi"])
        with open(os.path.join(stick, "EFI", "BOOT", "BOOTX64.EFI"),
                  "rb") as handle:
            self.assertEqual(handle.read(), b"shimx64.efi")
        with open(os.path.join(stick, "EFI", "fedora", "grub.cfg")) as f:
            config = f.read()
        self.assertIn("kss.stick=ABCD-1234", config)
        self.assertIn("search --no-floppy --fs-uuid --set=root ABCD-1234",
                      config)
        with open(os.path.join(stick, "kss", "initrd.img"), "rb") as f:
            self.assertEqual(f.read(), b"I" * 300000)
        self.assertTrue(os.path.isdir(os.path.join(
            stick, "KSysSupervisor", "state")))
        self.assertEqual(self.steps(), ["erase", "table", "mkfs", "copy",
                                        "verify"])
        # Read back from a mount of its own, read-only.
        self.assertIn("ro", self.tools.of("mount")[-1][3])

    def steps(self):
        return [e["key"] for e in self.events() if e.get("event") == "step"]

    def test_without_anything_signed_grub_is_built_and_says_so(self):
        self.module.EFI_ROOTS = ()
        modules = os.path.join(self.root, "grubmods")
        os.makedirs(modules)
        open(os.path.join(modules, "linux.mod"), "w").close()
        self.module.GRUB_MODULE_DIRS = (modules,)

        real = self.helper.run_tool

        def run(argv, *args, **kwargs):
            if os.path.basename(argv[0]) == "grub2-mkimage":
                with open(argv[argv.index("-o") + 1], "wb") as handle:
                    handle.write(b"built")
            return real(argv, *args, **kwargs)
        self.helper.run_tool = run
        result = self.helper.op_rescue_write(self.write())
        self.assertFalse(result["secure_boot"])
        self.assertEqual(sorted(os.listdir(os.path.join(
            self.copy, "EFI", "BOOT"))), ["BOOTX64.EFI", "grub.cfg"])
        self.assertIn("-p", self.tools.of("grub2-mkimage")[-1])

    def test_no_boot_loader_at_all_is_refused_before_anything_changes(self):
        self.module.EFI_ROOTS = ()
        self.tools.missing.update({"grub2-mkimage", "grub-mkimage"})
        self.assertIn("No boot loader", self.refused(self.write()))
        self.assertEqual(self.destructive(), [])

    def test_a_file_that_changed_or_is_missing_is_refused(self):
        files = json.loads(json.dumps(self.files))
        files["initrd.img"]["size"] += 1
        self.assertIn("changed", self.refused(self.write(files=files)))
        os.remove(os.path.join(self.stage, "vmlinuz"))
        self.assertIn("could not be opened", self.refused(self.write()))
        self.assertEqual(self.destructive(), [])

    def test_a_symlink_is_not_followed(self):
        path = os.path.join(self.stage, "vmlinuz")
        os.remove(path)
        os.symlink("/etc/passwd", path)
        self.assertIn("could not be opened", self.refused(self.write()))

    def test_a_file_that_is_not_the_callers_is_refused(self):
        with mock.patch.dict(os.environ, {"PKEXEC_UID": "65534"}):
            if self.module.invoking_user() is None:
                self.skipTest("no user 65534 on this system")
            self.assertIn("does not belong to you",
                          self.refused(self.write()))

    def test_a_checksum_that_differs_is_refused(self):
        files = json.loads(json.dumps(self.files))
        files["vmlinuz"]["sha256"] = "0" * 64
        self.assertIn("checksum differs", self.refused(self.write(
            files=files)))

    def test_the_system_disk_a_partition_and_a_small_drive_are_refused(self):
        request = self.write()
        request.update(device=os.path.join(self.tree.dev, "nvme0n1"),
                       identity=self.tree.identity("nvme0n1"))
        self.assertIn("running system", self.refused(request))
        request.update(device=os.path.join(self.tree.dev, "sdd1"),
                       identity=self.tree.identity("sdd1"))
        self.assertIn("whole drive", self.refused(request))
        self.tree.add_disk("sdz", 512 * MIB, diskseq=30)
        request.update(device=os.path.join(self.tree.dev, "sdz"),
                       identity=self.tree.identity("sdz"))
        self.assertIn("too small", self.refused(request))


class StickProtectionTest(HelperCase):
    """On the stick, the stick is the system's own drive - even when it
    could not be mounted - for the window and for the helper alike."""

    def setUp(self):
        super().setUp()
        folder = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        self.marker = os.path.join(folder, "kss-rescue")
        self.cmdline = os.path.join(folder, "cmdline")
        with open(self.cmdline, "w") as handle:
            handle.write("BOOT_IMAGE=/kss/vmlinuz kss.stick=ABCD-1234 quiet\n")
        by_uuid = os.path.join(self.tree.dev, "disk", "by-uuid")
        os.makedirs(by_uuid)
        os.symlink("../../sdd1", os.path.join(by_uuid, "ABCD-1234"))
        self.module.CMDLINE = self.cmdline
        self.module.RESCUE_MARKER = self.marker

    def test_the_helper_will_not_touch_it(self):
        request = self.request("sdd", table="gpt")
        self.helper.op_format(dict(request))   # no marker: a stick like any
        request = self.request("sdd", table="gpt")
        open(self.marker, "w").close()
        self.assertIn("recovery stick", self.refused(request))

    def test_the_window_shows_it_as_the_systems_drive(self):
        found = disks.parse(desktop(), fstab=[]) + parse(stick())
        uuid = [v.uuid for d in found if d.name == "sde"
                for v in d.volumes][0]
        with open(self.cmdline, "w") as handle:
            handle.write("kss.stick=%s\n" % uuid)
        self.assertEqual(disks.stick_uuid(self.marker, self.cmdline), "")
        open(self.marker, "w").close()
        self.assertEqual(disks.stick_uuid(self.marker, self.cmdline), uuid)
        marked = {d.name: d for d in disks.mark_stick(found, uuid)}
        self.assertTrue(marked["sde"].is_system)
        self.assertFalse(diskcopy.stick_availability(
            marked["sde"], marker="/nonexistent"))
        self.assertFalse(marked["sdb"].is_system)


class StickAvailabilityTest(unittest.TestCase):
    def setUp(self):
        found = disks.parse(desktop(), fstab=[]) + parse(stick())
        self.machine = {d.name: d for d in found}

    def test_a_usb_stick_can_be_one_the_system_disk_and_volumes_not(self):
        no_marker = "/nonexistent/kss-rescue"
        self.assertTrue(diskcopy.stick_availability(
            self.machine["sde"], marker=no_marker))
        self.assertIn("running system", diskcopy.stick_availability(
            self.machine["nvme0n1"], marker=no_marker).reason)
        volume = self.machine["sde"].volume("sde1")
        self.assertIn("whole drive", diskcopy.stick_availability(
            self.machine["sde"], volume, marker=no_marker).reason)
        with tempfile.NamedTemporaryFile() as marker:
            self.assertIn("recovery stick itself",
                          diskcopy.stick_availability(
                              self.machine["sde"],
                              marker=marker.name).reason)

    def test_the_plan_names_what_is_lost_and_the_helpers_steps(self):
        plan = diskcopy.plan_recovery_stick(self.machine["sde"],
                                            identities={"sde": {"n": 1}})
        self.assertEqual(plan.request["op"], "rescue_write")
        self.assertEqual(plan.request["identity"], {"n": 1})
        self.assertEqual(plan.step_keys, ["erase", "table", "mkfs", "copy",
                                          "verify"])
        self.assertFalse(plan.internal)
        self.assertTrue(plan.loses)

    def test_the_limits_are_the_helpers(self):
        self.assertEqual(diskcopy.STICK_MIN, load_helper().RESCUE_MIN)
        self.assertEqual(rescuebuild.STICK_LABEL, load_helper().RESCUE_LABEL)
        self.assertEqual(rescuebuild.STICK_FOLDER,
                         load_helper().RESCUE_FOLDER)


class StickDialogTest(DiskWindowCase):
    def dialog(self, disk="sde"):
        from ksyssupervisor.rescueui import RecoveryStickDialog
        found = {d.name: d for d in self.machine}
        return RecoveryStickDialog(found.get(disk), self.machine,
                                   identities={}, keyboard=("it", ""))

    def test_the_drive_it_was_opened_on_is_chosen_and_others_say_why(self):
        dialog = self.dialog()
        self.assertEqual(dialog.target.currentData(), "sde")
        self.assertIn("Will be erased", dialog.target_note.text())
        row = dialog.target.findData("nvme0n1")
        item = dialog.target.model().item(row)
        self.assertFalse(item.isEnabled())
        self.assertIn("running system", item.toolTip())
        self.assertEqual(dialog.keyboard_choice(), ("it", ""))
        self.assertIn("this computer's", dialog.keyboard.currentText())
        plan = dialog.build_plan()
        self.assertEqual(plan.request["device"], "/dev/sde")

    def test_from_the_menu_nothing_is_chosen_for_the_user(self):
        dialog = self.dialog(None)
        self.assertIsNone(dialog.target.currentData())
        self.assertFalse(dialog.start.isEnabled())
        self.assertIsNone(dialog.build_plan())

    def test_from_the_main_menu_it_waits_for_the_drives(self):
        # The Tools menu opens the Disk Utility and the dialog together.
        # With the drives still being read, the dialog listed none and said
        # "No drive here can take the stick".
        window = self.window()
        window.disks, window.real_disks = [], []
        window._have_read = False
        window.recovery_stick_selected(choose=True)
        window.recovery_stick_selected(choose=True)
        self.assertEqual(self.dialogs, [])
        window.refresh()
        self.app.processEvents()
        self.assertEqual(len(self.dialogs), 1)
        names = [disk.name for disk, answer in self.dialogs[0].candidates
                 if answer]
        self.assertIn("sde", names)

    def test_it_fits(self):
        dialog = self.dialog()
        dialog.show()
        self.addCleanup(dialog.close)
        self.app.processEvents()
        self.assert_dialog_fits(dialog)


if __name__ == "__main__":
    unittest.main()
