"""The update check and self-update, without a network or a real install.

Everything that would touch GitHub goes through a fake opener, and everything
that would install runs against temporary directories with a recorded runner.
"""

import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from ksyssupervisor import APP_VERSION, installation, updater


class FakeResponse(io.BytesIO):
    def __init__(self, data, length=True):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))} if length else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    """Serves bytes by URL and records what was asked for."""

    def __init__(self, files):
        self.files = files
        self.requested = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.requested.append(url)
        if url not in self.files:
            raise updater.urllib.error.HTTPError(url, 404, "nope", {}, None)
        return FakeResponse(self.files[url])


def release_json(tag="v9.9.9", assets=("SHA256SUMS",), **extra):
    data = {
        "tag_name": tag,
        "body": "What changed",
        "html_url": "https://github.com/x/y/releases/tag/%s" % tag,
        "draft": False,
        "prerelease": False,
        "assets": [{"name": name,
                    "browser_download_url":
                        "https://github.com/x/y/releases/download/%s/%s" % (tag, name)}
                   for name in assets],
    }
    data.update(extra)
    return data


class VersionTest(unittest.TestCase):
    def test_parses_plain_and_tagged_versions(self):
        self.assertEqual(updater.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_version("v1.2"), (1, 2, 0))
        self.assertIsNone(updater.parse_version("latest"))
        self.assertIsNone(updater.parse_version(""))

    def test_orders_numerically_not_as_text(self):
        self.assertTrue(updater.is_newer("1.10.0", "1.9.0"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(updater.is_newer("0.9", "1.0.0"))

    def test_the_running_version_parses(self):
        self.assertIsNotNone(updater.parse_version(APP_VERSION))


class ReleaseTest(unittest.TestCase):
    def test_reads_the_github_answer(self):
        name = updater.tarball_asset("9.9.9")
        release = updater.parse_release(release_json(assets=(name, "SHA256SUMS")))
        self.assertEqual(release.version, "9.9.9")
        self.assertEqual(release.asset_for(installation.USER), name)
        self.assertEqual(release.asset_for(installation.SYSTEM), name)
        self.assertIsNone(release.asset_for(installation.APPIMAGE))
        self.assertIsNone(release.asset_for(installation.SOURCE))

    def test_a_draft_or_prerelease_is_not_a_release(self):
        for flag in ("draft", "prerelease"):
            with self.assertRaises(updater.UpdateError):
                updater.parse_release(release_json(**{flag: True}))

    def test_a_tag_that_is_not_a_version_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.parse_release(release_json(tag="nightly"))

    def test_check_reports_only_a_newer_release(self):
        url = updater.API_URL % updater.GITHUB_REPO
        newer = FakeOpener({url: json.dumps(release_json("v99.0.0")).encode()})
        self.assertEqual(updater.check(newer).version, "99.0.0")
        same = FakeOpener({url: json.dumps(release_json("v" + APP_VERSION)).encode()})
        self.assertIsNone(updater.check(same))

    def test_no_release_yet_is_a_readable_error(self):
        with self.assertRaises(updater.UpdateError) as caught:
            updater.check(FakeOpener({}))
        self.assertIn("No release", str(caught.exception))


class UrlTest(unittest.TestCase):
    def test_only_github_over_https(self):
        self.assertTrue(updater.url_allowed("https://api.github.com/repos/a/b"))
        self.assertTrue(updater.url_allowed(
            "https://objects.githubusercontent.com/x"))
        self.assertFalse(updater.url_allowed("http://github.com/a"))
        self.assertFalse(updater.url_allowed("https://github.com.evil.example/a"))
        self.assertFalse(updater.url_allowed("https://example.com/a"))

    def test_a_redirect_elsewhere_is_refused(self):
        handler = updater._CheckedRedirects()
        with self.assertRaises(updater.UpdateError):
            handler.redirect_request(None, None, 302, "", {},
                                     "https://example.com/evil")


class ChecksumTest(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b"payload")
        handle.close()
        self.path = handle.name
        self.addCleanup(os.remove, self.path)

    def test_parses_sha256sum_output(self):
        digest = "a" * 64
        sums = updater.parse_sums("%s  one.tar.gz\n%s *two.AppImage\nnoise\n"
                                  % (digest, digest))
        self.assertEqual(sums, {"one.tar.gz": digest, "two.AppImage": digest})

    def test_a_matching_file_passes(self):
        updater.verify(self.path, {"f": updater.sha256(self.path)}, "f")

    def test_a_mismatch_or_missing_entry_aborts(self):
        with self.assertRaises(updater.UpdateError):
            updater.verify(self.path, {"f": "0" * 64}, "f")
        with self.assertRaises(updater.UpdateError):
            updater.verify(self.path, {}, "f")


def make_tarball(path, entries):
    """entries: {name: bytes or None for a directory}, or TarInfo objects."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif isinstance(data, str):         # a symlink target
                info.type = tarfile.SYMTYPE
                info.linkname = data
                tar.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = os.path.join(self.tmp.name, "r.tar.gz")
        self.dest = os.path.join(self.tmp.name, "out")
        os.mkdir(self.dest)

    def test_a_release_unpacks_to_its_folder(self):
        make_tarball(self.archive, {"KSysSupervisor-9.9.9": None,
                                    "KSysSupervisor-9.9.9/install.sh": b"#!/bin/sh\n"})
        root = updater.safe_extract(self.archive, self.dest)
        self.assertTrue(os.path.isfile(os.path.join(root, "install.sh")))

    def test_path_traversal_is_refused(self):
        make_tarball(self.archive, {"KSysSupervisor-9.9.9/install.sh": b"x",
                                    "KSysSupervisor-9.9.9/../../evil": b"x"})
        with self.assertRaises(updater.UpdateError):
            updater.safe_extract(self.archive, self.dest)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil")))

    def test_links_are_refused(self):
        make_tarball(self.archive, {"KSysSupervisor-9.9.9/install.sh": "/etc/passwd"})
        with self.assertRaises(updater.UpdateError):
            updater.safe_extract(self.archive, self.dest)

    def test_an_archive_without_install_sh_is_refused(self):
        make_tarball(self.archive, {"KSysSupervisor-9.9.9/README.md": b"x"})
        with self.assertRaises(updater.UpdateError):
            updater.safe_extract(self.archive, self.dest)


class InstallKindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def env(self, **extra):
        env = {"HOME": self.home, "PATH": "/nonexistent"}
        env.update(extra)
        return env

    def test_a_checkout_is_source(self):
        self.assertEqual(installation.kind(self.env(), "/some/checkout"),
                         installation.SOURCE)

    def test_the_user_install_dir(self):
        app_dir = os.path.join(self.home, ".local", "share", "ksyssupervisor")
        os.makedirs(app_dir)
        self.assertEqual(installation.kind(self.env(), app_dir), installation.USER)
        self.assertEqual(installation.launcher(installation.USER, self.env()),
                         os.path.join(self.home, ".local", "bin", "ksyssupervisor"))

    def test_xdg_data_home_moves_the_user_install(self):
        data = os.path.join(self.home, "data")
        app_dir = os.path.join(data, "ksyssupervisor")
        os.makedirs(app_dir)
        self.assertEqual(installation.kind(self.env(XDG_DATA_HOME=data), app_dir),
                         installation.USER)

    def test_the_system_install_dir(self):
        self.assertEqual(installation.kind(self.env(), installation.SYSTEM_DIR),
                         installation.SYSTEM)

    def test_an_appimage_needs_the_package_inside_its_mount(self):
        image = os.path.join(self.home, "KSysSupervisor.AppImage")
        open(image, "w").close()
        appdir = os.path.join(self.home, "mnt")
        inside = os.path.join(appdir, "usr", "share", "ksyssupervisor")
        os.makedirs(inside)
        env = self.env(APPIMAGE=image, APPDIR=appdir)
        self.assertEqual(installation.kind(env, inside), installation.APPIMAGE)
        # Inherited from an AppImage that started this one: not ours.
        self.assertEqual(installation.kind(env, "/some/checkout"),
                         installation.SOURCE)

    def test_self_command_for_a_checkout_names_the_script(self):
        command = installation.self_command(self.env(), "/some/checkout")
        self.assertEqual(command, "python3 /some/checkout/KSysSupervisor.py")

    def test_self_command_for_an_install_off_path_is_the_full_launcher(self):
        app_dir = os.path.join(self.home, ".local", "share", "ksyssupervisor")
        os.makedirs(app_dir)
        self.assertEqual(installation.self_command(self.env(), app_dir),
                         installation.user_launcher(self.env()))

    def test_self_command_uses_the_short_name_when_it_resolves_here(self):
        app_dir = os.path.join(self.home, ".local", "share", "ksyssupervisor")
        os.makedirs(app_dir)
        bin_dir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bin_dir)
        launcher = os.path.join(bin_dir, "ksyssupervisor")
        with open(launcher, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(launcher, 0o755)
        self.assertEqual(installation.self_command(self.env(PATH=bin_dir), app_dir),
                         "ksyssupervisor")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, data=b"x"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_an_appimage_is_replaced_and_renamed(self):
        old = self.write("KSysSupervisor-%s-x86_64.AppImage" % APP_VERSION, b"old")
        new = self.write("download", b"new")
        target = updater.apply_appimage(new, old, "9.9.9")
        self.assertEqual(os.path.basename(target), "KSysSupervisor-9.9.9-x86_64.AppImage")
        self.assertFalse(os.path.exists(old))
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"new")
        self.assertTrue(os.access(target, os.X_OK))

    def test_an_appimage_without_a_version_keeps_its_name(self):
        old = self.write("ksys.AppImage", b"old")
        new = self.write("download", b"new")
        self.assertEqual(updater.apply_appimage(new, old, "9.9.9"), old)

    def _release_tarball(self):
        path = os.path.join(self.tmp.name, "r.tar.gz")
        make_tarball(path, {"KSysSupervisor-9.9.9": None,
                            "KSysSupervisor-9.9.9/install.sh": b"#!/bin/sh\n"})
        return path

    def test_a_user_install_reruns_install_sh_without_a_password(self):
        calls = []
        updater.apply_python(self._release_tarball(), installation.USER,
                             runner=lambda cmd: calls.append(cmd) or (0, ""))
        self.assertEqual(calls[0][0], "bash")
        self.assertEqual(calls[0][2:], ["--user", "--yes"])

    def test_a_system_install_goes_through_pkexec(self):
        calls = []
        updater.apply_python(self._release_tarball(), installation.SYSTEM,
                             runner=lambda cmd: calls.append(cmd) or (0, ""))
        self.assertEqual(calls[0][:2], ["pkexec", "/bin/bash"])
        self.assertEqual(calls[0][3:], ["--system", "--yes"])

    def test_a_dismissed_password_prompt_is_reported(self):
        with self.assertRaises(updater.UpdateError) as caught:
            updater.apply_python(self._release_tarball(), installation.SYSTEM,
                                 runner=lambda cmd: (126, ""))
        self.assertIn("not authorised", str(caught.exception))

    def test_a_failed_installer_is_reported_with_its_output(self):
        with self.assertRaises(updater.UpdateError) as caught:
            updater.apply_python(self._release_tarball(), installation.USER,
                                 runner=lambda cmd: (1, "disk full"))
        self.assertIn("disk full", str(caught.exception))


class PerformUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _served(self, payload, sums_for=None):
        name = updater.appimage_asset("9.9.9")
        release = updater.parse_release(release_json(assets=(name, "SHA256SUMS")))
        digest = updater.hashlib.sha256(sums_for or payload).hexdigest()
        opener = FakeOpener({
            release.assets[name]: payload,
            release.assets["SHA256SUMS"]: ("%s  %s\n" % (digest, name)).encode(),
        })
        old = os.path.join(self.tmp.name, "KSysSupervisor-%s-x86_64.AppImage" % APP_VERSION)
        with open(old, "wb") as handle:
            handle.write(b"old")
        return release, opener, old

    def test_an_appimage_update_end_to_end(self):
        release, opener, old = self._served(b"new AppImage")
        stages = []
        launcher = updater.perform_update(
            release, installation.APPIMAGE, opener=opener,
            progress=lambda text, done, total: stages.append(text),
            env={"APPIMAGE": old})
        with open(launcher, "rb") as handle:
            self.assertEqual(handle.read(), b"new AppImage")
        self.assertIn("Verifying the download...", stages)

    def test_a_tampered_download_changes_nothing(self):
        release, opener, old = self._served(b"evil", sums_for=b"genuine")
        with self.assertRaises(updater.UpdateError):
            updater.perform_update(release, installation.APPIMAGE,
                                   opener=opener, env={"APPIMAGE": old})
        with open(old, "rb") as handle:
            self.assertEqual(handle.read(), b"old")

    def test_a_release_without_checksums_is_not_installed(self):
        name = updater.appimage_asset("9.9.9")
        release = updater.parse_release(release_json(assets=(name,)))
        with self.assertRaises(updater.UpdateError):
            updater.perform_update(release, installation.APPIMAGE,
                                   opener=FakeOpener({}), env={"APPIMAGE": "x"})

    def test_a_checkout_is_never_replaced(self):
        release = updater.parse_release(release_json())
        with self.assertRaises(updater.UpdateError) as caught:
            updater.perform_update(release, installation.SOURCE)
        self.assertIn("git", str(caught.exception))


class HelperAfterUpdateTest(unittest.TestCase):
    def test_no_installed_helper_needs_no_update(self):
        with mock.patch("ksyssupervisor.fanctl.helper_path", return_value=None):
            self.assertFalse(updater.helper_outdated())

    def test_a_differing_helper_is_outdated(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("PROTOCOL_VERSION = 1\n")
        self.addCleanup(os.remove, handle.name)
        with mock.patch("ksyssupervisor.fanctl.helper_path",
                        return_value=handle.name):
            self.assertTrue(updater.helper_outdated())

    def test_the_same_helper_is_current(self):
        from ksyssupervisor import fanctl
        bundled, _ = fanctl.bundled_helper()
        with mock.patch("ksyssupervisor.fanctl.helper_path", return_value=bundled):
            self.assertFalse(updater.helper_outdated())


if __name__ == "__main__":
    unittest.main()
