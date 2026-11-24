"""The privileged helpers this application ships, and how they get installed.

Two features need root: fan control and the disk utility. Each has a small
standalone helper script and a polkit action that authorises exactly that
script. What the two share is here - where a helper is looked for, which
version an installed one is, and installing them system-wide with one password
prompt - while each feature's own module keeps its protocol and its client.

Standard library only, and no Qt.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from . import log

#: Where a helper may live. Both are root-owned and not writable by the user,
#: which is the point: the polkit action authorises a path, and a file the
#: user could replace would authorise whatever they put there. /usr/local
#: first, because that is where software outside the package manager belongs.
INSTALL_DIRS = ("/usr/local/lib/ksyssupervisor", "/usr/lib/ksyssupervisor")
POLICY_DIR = "/usr/share/polkit-1/actions"

#: The directory holding the ksyssupervisor package, helpers/ and packaging/.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_VERSION_RE = re.compile(r"^PROTOCOL_VERSION\s*=\s*([0-9]+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class HelperSpec:
    key: str                 # "fan", "disk"
    helper_name: str         # file name under helpers/
    policy_name: str         # file name under packaging/

    @property
    def paths(self):
        """Where an installed copy is looked for, in order."""
        return tuple(os.path.join(d, self.helper_name) for d in INSTALL_DIRS)

    @property
    def install_path(self):
        return self.paths[0]

    @property
    def policy_install_path(self):
        return os.path.join(POLICY_DIR, self.policy_name)

    def bundled(self, root=None):
        """(helper, policy) shipped with this copy, or (None, None).

        An AppImage carries both, because there is nowhere else for its user
        to get them from; a source checkout and an installation have them too.
        """
        root = APP_ROOT if root is None else root
        helper = os.path.join(root, "helpers", self.helper_name)
        policy = os.path.join(root, "packaging", self.policy_name)
        if os.path.isfile(helper) and os.path.isfile(policy):
            return helper, policy
        return None, None

    def installed_path(self):
        """The installed helper, or None."""
        for path in self.paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None


FAN = HelperSpec("fan", "ksyssupervisor-fanhelper",
                 "org.ksyssupervisor.fancontrol.policy")
DISK = HelperSpec("disk", "ksyssupervisor-diskhelper",
                  "org.ksyssupervisor.diskmanager.policy")
ALL = (FAN, DISK)


def protocol_version(path):
    """The PROTOCOL_VERSION a helper script declares, or None if unknown.

    A helper is a world-readable script whose version is a plain constant, so
    a mismatch can be seen for free, before a password prompt has been spent
    on starting it. None means the file could not be read or states no
    version; the handshake stays the authority then.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(65536)
    except OSError:
        return None
    match = _VERSION_RE.search(text)
    return int(match.group(1)) if match else None


#: Run as root, once per helper. Installed with the mode a helper needs and no
#: more: root-owned and not writable by anyone else.
_INSTALL_LINES = """\
install -Dm755 -o root -g root '%(helper_src)s' '%(helper_dst)s'
sed 's|@HELPER_PATH@|%(helper_dst)s|' '%(policy_src)s' > '%(policy_dst)s'
chown root:root '%(policy_dst)s'
chmod 644 '%(policy_dst)s'
"""


def run_pkexec(command):
    try:
        done = subprocess.run(command, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
    except Exception as exc:                 # pragma: no cover - environmental
        log.exception("Could not run pkexec")
        return 1, str(exc)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def install(specs=ALL, runner=None):
    """Install every helper in `specs` that this copy ships. (code, output).

    All of them under one pkexec, so the user is asked once however many
    there are. The files are staged into a temporary directory first and
    installed from there, never straight out of the directory they ship in:
    inside an AppImage that is a FUSE mount owned by the user, which root
    cannot read, so copying as root from it would fail with a permission
    error that has nothing to do with the password.

    The exit code is pkexec's: 0 done, 126 not authorised, 127 dismissed.
    """
    with tempfile.TemporaryDirectory(prefix="ksyssupervisor-helper-") as stage:
        # World-readable: root reads these back out of the staging directory.
        os.chmod(stage, 0o755)
        lines = ["set -e\n"]
        count = 0
        for spec in specs:
            helper, policy = spec.bundled()
            if helper is None:
                continue
            suffix = "" if count == 0 else "-%s" % spec.key
            staged_helper = os.path.join(stage, "helper" + suffix)
            staged_policy = os.path.join(stage, "policy" + suffix)
            shutil.copyfile(helper, staged_helper)
            shutil.copyfile(policy, staged_policy)
            os.chmod(staged_helper, 0o755)
            os.chmod(staged_policy, 0o644)
            lines.append(_INSTALL_LINES % {
                "helper_src": staged_helper,
                "policy_src": staged_policy,
                "helper_dst": spec.install_path,
                "policy_dst": spec.policy_install_path,
            })
            count += 1
        if count == 0:
            return 1, "This copy ships no helper to install."

        script = os.path.join(stage, "install.sh")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
        os.chmod(script, 0o755)

        run = runner or run_pkexec
        return run(["pkexec", "/bin/sh", script])


def differs(installed, bundled):
    """Whether an installed helper is not the one this copy ships.

    Compared byte for byte: a helper changed without its protocol version
    moving still has to follow the application. False when either is missing -
    there is nothing to update, or nothing to update it from.
    """
    if installed is None or bundled is None:
        return False
    try:
        with open(installed, "rb") as a, open(bundled, "rb") as b:
            return a.read() != b.read()
    except OSError:
        return False
