"""Checking GitHub for a newer release, and replacing this copy with it.

Every release publishes the same three assets, and this module relies on their
names:

    KSysSupervisor-<version>-x86_64.AppImage
    KSysSupervisor-<version>.tar.gz          (the Python version, with install.sh)
    SHA256SUMS

An update replaces the copy in the way it was installed (see installation.py):
an AppImage is swapped for the new file, an install.sh installation is
re-installed by the new release's own install.sh - under pkexec when it is
system-wide, which is the one password prompt. Settings are not touched by
either: they live in ~/.config/KSysSupervisor, which no installer writes. A
source checkout is only told about the release; replacing a working tree is
git's job.

Nothing is installed that does not match the release's SHA256SUMS. Qt-free and
standard library only, like fanctl.py, so it is testable without a display or
a network.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict

from . import APP_NAME, APP_VERSION, GITHUB_REPO, installation, log

API_URL = "https://api.github.com/repos/%s/releases/latest"
SUMS_ASSET = "SHA256SUMS"

#: The only hosts anything is fetched from. Release downloads redirect from
#: github.com to a *.githubusercontent.com storage host; every hop is checked.
ALLOWED_HOSTS = ("github.com", "api.github.com")
ALLOWED_SUFFIXES = (".githubusercontent.com",)

TIMEOUT = 15
#: A release is a few tens of MB; anything far past that is not ours.
MAX_DOWNLOAD = 512 * 1024 * 1024
CHUNK = 256 * 1024

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")


class UpdateError(Exception):
    """The check or the update failed; the message is for the user."""


def parse_version(text):
    """(major, minor, patch) from "1.2.3" or "v1.2", or None if not a version."""
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def is_newer(remote, local=APP_VERSION):
    remote, local = parse_version(remote), parse_version(local)
    return remote is not None and local is not None and remote > local


def appimage_asset(version):
    return "%s-%s-x86_64.AppImage" % (APP_NAME, version)


def tarball_asset(version):
    return "%s-%s.tar.gz" % (APP_NAME, version)


@dataclass
class Release:
    version: str
    tag: str
    notes: str = ""
    page_url: str = ""
    assets: Dict[str, str] = field(default_factory=dict)

    def asset_for(self, install_kind):
        """The asset that updates an installation of this kind, or None."""
        if install_kind == installation.APPIMAGE:
            name = appimage_asset(self.version)
        elif install_kind in (installation.USER, installation.SYSTEM):
            name = tarball_asset(self.version)
        else:
            return None
        return name if name in self.assets else None


# ---- network ----------------------------------------------------------------

def url_allowed(url):
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (
        host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES))


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to a host the first request could have gone to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl):
            raise UpdateError("The download was redirected to an unexpected "
                              "address (%s)." % urllib.parse.urlsplit(newurl).hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, opener=None, timeout=TIMEOUT, accept=None):
    if not url_allowed(url):
        raise UpdateError("Refusing to download from %s." % url)
    opener = opener or urllib.request.build_opener(_CheckedRedirects())
    headers = {"User-Agent": "%s/%s" % (APP_NAME, APP_VERSION)}
    if accept:
        headers["Accept"] = accept
    try:
        return opener.open(urllib.request.Request(url, headers=headers),
                           timeout=timeout)
    except UpdateError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No release has been published yet.")
        if exc.code == 403:
            raise UpdateError("GitHub refused the request (rate limit?). "
                              "Try again later.")
        raise UpdateError("GitHub answered with an error (%s)." % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError("Could not reach GitHub: %s" % reason)


def parse_release(data):
    """A Release from the GitHub API's JSON, or UpdateError if it is not one."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise UpdateError("No published release was found.")
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        raise UpdateError("The latest release has no version number (%r)." % tag)
    assets = {}
    for asset in data.get("assets") or ():
        name, url = asset.get("name"), asset.get("browser_download_url")
        if name and url:
            assets[str(name)] = str(url)
    return Release(version=tag.lstrip("v"), tag=tag,
                   notes=str(data.get("body") or ""),
                   page_url=str(data.get("html_url") or ""), assets=assets)


def fetch_latest(opener=None, repo=GITHUB_REPO):
    """The latest published release. Raises UpdateError."""
    with _open(API_URL % repo, opener,
               accept="application/vnd.github+json") as response:
        try:
            data = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise UpdateError("GitHub's answer could not be read.")
    return parse_release(data)


def check(opener=None):
    """The latest release if it is newer than this copy, else None."""
    release = fetch_latest(opener)
    return release if is_newer(release.version) else None


def download(url, dest, progress=None, opener=None):
    """Stream `url` into `dest`. progress(done, total) where total may be 0."""
    with _open(url, opener) as response, open(dest, "wb") as out:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError, AttributeError):
            total = 0
        done = 0
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_DOWNLOAD:
                raise UpdateError("The download is far larger than a release.")
            out.write(chunk)
            if progress:
                progress(done, total)
    return dest


# ---- verification -------------------------------------------------------------

def parse_sums(text):
    """{filename: sha256} from `sha256sum` output."""
    sums = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0].lower()
    return sums


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path, sums, name):
    expected = sums.get(name)
    if expected is None:
        raise UpdateError("%s is not listed in the release's checksums, so it "
                          "was not installed." % name)
    if sha256(path) != expected:
        raise UpdateError("%s does not match its published checksum - the "
                          "download is damaged or not the real release. "
                          "Nothing was changed." % name)


def safe_extract(archive, dest):
    """Unpack a release tarball, refusing anything that could land outside
    `dest`. Returns the single top-level directory it contains."""
    dest = os.path.realpath(dest)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        tops = set()
        for member in members:
            name = member.name
            if name.startswith("/") or ".." in name.split("/"):
                raise UpdateError("The archive contains an unsafe path: %s" % name)
            if not (member.isfile() or member.isdir()):
                raise UpdateError("The archive contains an unexpected entry: %s" % name)
            target = os.path.realpath(os.path.join(dest, name))
            if not (target == dest or target.startswith(dest + os.sep)):
                raise UpdateError("The archive contains an unsafe path: %s" % name)
            tops.add(name.split("/")[0])
        if len(tops) != 1:
            raise UpdateError("The archive is not laid out like a release.")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members, filter="data")
        else:                                           # Python < 3.12
            tar.extractall(dest, members)
    root = os.path.join(dest, tops.pop())
    if not os.path.isfile(os.path.join(root, "install.sh")):
        raise UpdateError("The archive has no install.sh.")
    return root


# ---- installing -------------------------------------------------------------

def _run(command):
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def apply_appimage(new_file, current, version):
    """Put the new AppImage where the old one was. Returns its path.

    The file keeps its name with the version in it swapped, so a directory of
    downloads does not end up with a 1.2 file that is really 1.3. Written beside
    the old one and renamed over, so a failure at any point leaves the old
    AppImage exactly as it was; the old file goes only once the new one is in
    place. The running copy is unaffected - it is already mounted.
    """
    folder, name = os.path.split(os.path.abspath(current))
    new_name = name.replace(APP_VERSION, version) if APP_VERSION in name else name
    target = os.path.join(folder, new_name)
    partial = os.path.join(folder, ".%s.part" % new_name)
    try:
        shutil.copyfile(new_file, partial)
        os.chmod(partial, 0o755)
        os.replace(partial, target)
    except OSError as exc:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise UpdateError("Could not write the new AppImage next to the old "
                          "one: %s" % exc)
    if os.path.abspath(target) != os.path.abspath(current):
        try:
            os.remove(current)
        except OSError as exc:
            log.warning("Could not remove the old AppImage %s: %s", current, exc)
    return target


def apply_python(archive, install_kind, runner=None):
    """Run the new release's install.sh the way this copy was installed."""
    runner = runner or _run
    stage = tempfile.mkdtemp(prefix="ksyssupervisor-update-")
    try:
        # Readable by root: a system-wide update runs install.sh through pkexec.
        os.chmod(stage, 0o755)
        root = safe_extract(archive, stage)
        script = os.path.join(root, "install.sh")
        if install_kind == installation.SYSTEM:
            command = ["pkexec", "/bin/bash", script, "--system", "--yes"]
        elif install_kind == installation.USER:
            command = ["bash", script, "--user", "--yes"]
        else:
            raise UpdateError("This copy was not installed by install.sh.")
        code, output = runner(command)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if code in (126, 127) and install_kind == installation.SYSTEM:
        raise UpdateError("The update was not authorised, so nothing was changed.")
    if code != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        raise UpdateError("The installer failed (exit %s).\n\n%s" % (code, tail))


def perform_update(release, install_kind=None, progress=None, opener=None,
                   runner=None, env=None):
    """Download, verify and install `release`. Returns the command to restart.

    progress(text, done, total) is called from whatever thread runs this.
    """
    install_kind = install_kind or installation.kind(env)
    env = os.environ if env is None else env
    name = release.asset_for(install_kind)
    if name is None:
        if install_kind == installation.SOURCE:
            raise UpdateError("This copy runs from a source folder, which is "
                              "updated with git or by downloading the release.")
        raise UpdateError("Release %s has no download for this kind of "
                          "installation." % release.version)
    if SUMS_ASSET not in release.assets:
        raise UpdateError("Release %s publishes no checksums, so it cannot be "
                          "installed safely." % release.version)

    def report(text, done=0, total=0):
        if progress:
            progress(text, done, total)

    with tempfile.TemporaryDirectory(prefix="ksyssupervisor-download-") as tmp:
        report("Downloading checksums...")
        sums_path = download(release.assets[SUMS_ASSET],
                             os.path.join(tmp, SUMS_ASSET), opener=opener)
        with open(sums_path, encoding="utf-8", errors="replace") as handle:
            sums = parse_sums(handle.read())

        path = os.path.join(tmp, name)
        download(release.assets[name], path, opener=opener,
                 progress=lambda done, total: report(
                     "Downloading %s..." % name, done, total))
        report("Verifying the download...")
        verify(path, sums, name)

        report("Installing %s %s..." % (APP_NAME, release.version))
        if install_kind == installation.APPIMAGE:
            return apply_appimage(path, env.get("APPIMAGE"), release.version)
        apply_python(path, install_kind, runner)
        return installation.launcher(install_kind, env)


# ---- the fan helper after an update -----------------------------------------

def helper_outdated():
    """Whether the installed fan helper differs from the one this copy ships.

    Asked by the new version on its first start after an update: an AppImage
    carries its helper inside, where the old version could not look before
    replacing itself. False when no helper is installed - nothing to update.
    """
    from . import fanctl
    installed = fanctl.helper_path()
    bundled, _policy = fanctl.bundled_helper()
    if installed is None or bundled is None:
        return False
    try:
        with open(installed, "rb") as a, open(bundled, "rb") as b:
            return a.read() != b.read()
    except OSError:
        return False
