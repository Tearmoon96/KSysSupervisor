#!/usr/bin/env bash
#
# KSysSupervisor installer.
#
# Works for a per-user install (default when run as a normal user) and a
# system-wide one (default under sudo). Fan control is deliberately only part
# of the system-wide install: its helper runs as root, so it has to live
# somewhere the authorised user cannot rewrite, or the polkit action would be
# a loophole rather than a control.

set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'
YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}${BOLD}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}${BOLD}[SUCCESS]${NC} $1"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARNING]${NC} $1"; }
error()   { echo -e "${RED}${BOLD}[ERROR]${NC} $1"; }

die() { error "$1"; exit 1; }

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_MODE="auto"
REBUILD_VENV=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --system) INSTALL_MODE="system" ;;
        --user)   INSTALL_MODE="user" ;;
        --rebuild-venv) REBUILD_VENV=1 ;;
        --fan-control) INSTALL_MODE="fan-control" ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h)
            cat <<'USAGE'
Usage: ./install.sh [OPTIONS]

Options:
  --user           Install for the current user only (default as a normal user)
  --system         Install for all users (default under sudo). Required for
                   fan control, which needs a root-owned helper.
  --rebuild-venv   Recreate the Python virtual environment from scratch
  --fan-control    Install ONLY the fan control helper and its polkit policy,
                   leaving an existing installation exactly where it is. Use
                   this to add fan control to a per-user install without
                   moving it: the app does not have to be system-wide, only
                   the helper does.
  --yes            Do not ask before replacing a running copy (used by the
                   in-app updater, which is that running copy)
  --help           Show this message
USAGE
            exit 0
            ;;
        *) warn "Unknown option '$arg', ignoring." ;;
    esac
done

if [ "$INSTALL_MODE" = "auto" ]; then
    if [ "$(id -u)" -eq 0 ]; then INSTALL_MODE="system"; else INSTALL_MODE="user"; fi
fi

# The fan-control-only mode installs no application files, so it needs none of
# these paths and must not announce an installation it is not performing.
if [ "$INSTALL_MODE" = "fan-control" ]; then
    :
elif [ "$INSTALL_MODE" = "system" ]; then
    [ "$(id -u)" -eq 0 ] || die "A system-wide install needs root: sudo ./install.sh --system"
    INSTALL_DIR="/opt/ksyssupervisor"
    BIN_DIR="/usr/local/bin"
    DESKTOP_DIR="/usr/share/applications"
    ICON_DIR="/usr/share/icons/hicolor/scalable/apps"
    PIXMAP_DIR="/usr/share/pixmaps"
    info "Installing ${BOLD}system-wide${NC}..."
else
    USER_HOME="${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}"
    DATA_HOME="${XDG_DATA_HOME:-$USER_HOME/.local/share}"
    INSTALL_DIR="$DATA_HOME/ksyssupervisor"
    BIN_DIR="$USER_HOME/.local/bin"
    DESKTOP_DIR="$DATA_HOME/applications"
    ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
    PIXMAP_DIR="$DATA_HOME/pixmaps"
    info "Installing ${BOLD}for $(id -un)${NC}..."
fi

# ---- fan control helper -----------------------------------------------------

HELPER_SRC="$SOURCE_DIR/helpers/ksyssupervisor-fanhelper"
POLICY_SRC="$SOURCE_DIR/packaging/org.ksyssupervisor.fancontrol.policy"
HELPER_DIR="/usr/local/lib/ksyssupervisor"
HELPER_FILE="$HELPER_DIR/ksyssupervisor-fanhelper"
POLICY_FILE="/usr/share/polkit-1/actions/org.ksyssupervisor.fancontrol.policy"
FAN_CONTROL="no"

# Names this project used before its first public release. Left in place they
# would be an orphaned root-owned helper still authorised by an orphaned polkit
# action - harmless today, but a privileged binary nothing maintains is exactly
# what should not accumulate on a system.
LEGACY_HELPER_DIR="/usr/local/lib/ksysmonitor"
LEGACY_POLICY="/usr/share/polkit-1/actions/org.ksysmonitor.fancontrol.policy"

remove_legacy_fan_control() {
    local found=0
    [ -e "$LEGACY_POLICY" ] && { rm -f "$LEGACY_POLICY" && found=1; }
    [ -e "$LEGACY_HELPER_DIR" ] && { rm -rf "$LEGACY_HELPER_DIR" && found=1; }
    [ "$found" -eq 1 ] && info "Removed the fan control helper installed under the old name."
    return 0
}

# Installs the two root-owned files fan control needs, and nothing else.
#
# Kept separate from the application install because the two are genuinely
# independent: the app looks for the helper at a fixed path and does not care
# where it is running from itself. That is what lets a per-user install gain
# fan control without being moved or reinstalled.
install_fan_control() {
    remove_legacy_fan_control
    [ -f "$HELPER_SRC" ] && [ -f "$POLICY_SRC" ] || {
        warn "Fan control sources are missing; skipping it."
        return 1
    }

    info "Installing the fan control helper and its polkit policy..."

    # Checked before it is trusted with root: this is the one file in the
    # install that will run with full privileges.
    python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$HELPER_SRC" \
        || die "The fan helper does not parse; refusing to install it as root."

    mkdir -p "$HELPER_DIR"
    install -m 0755 -o root -g root "$HELPER_SRC" "$HELPER_FILE" \
        || die "Could not install the fan helper."

    mkdir -p "$(dirname "$POLICY_FILE")"
    sed "s|@HELPER_PATH@|$HELPER_FILE|g" "$POLICY_SRC" > "$POLICY_FILE" \
        || die "Could not install the polkit policy."
    chmod 644 "$POLICY_FILE"
    chown root:root "$POLICY_FILE"

    # The policy names the helper by absolute path; if the two disagree,
    # pkexec refuses with a message that explains none of this.
    grep -q "$HELPER_FILE" "$POLICY_FILE" \
        || die "The polkit policy does not point at $HELPER_FILE."

    if command -v pkaction >/dev/null 2>&1; then
        pkaction --action-id org.ksyssupervisor.fancontrol.manage >/dev/null 2>&1 \
            || warn "polkit has not picked up the new action yet; it will after a reboot."
    fi

    if command -v pkexec >/dev/null 2>&1; then
        FAN_CONTROL="yes"
    else
        warn "Fan control needs 'pkexec', which is not installed."
        echo "         Install polkit: Fedora 'sudo dnf install polkit',"
        echo "         Debian/Ubuntu 'sudo apt install pkexec', Arch 'sudo pacman -S polkit'."
    fi
    return 0
}

# ---- fan-control-only mode --------------------------------------------------
#
# Handled before anything else, and exits: it deliberately touches nothing but
# the two root-owned files, so an existing installation - wherever it lives -
# is left exactly as it was.
if [ "$INSTALL_MODE" = "fan-control" ]; then
    [ "$(id -u)" -eq 0 ] || die "Installing the fan helper needs root: sudo ./install.sh --fan-control"

    install_fan_control || die "Fan control could not be installed."

    [ -x "$HELPER_FILE" ] || die "$HELPER_FILE is not executable."
    [ -f "$POLICY_FILE" ] || die "$POLICY_FILE is missing."
    owner=$(stat -c '%U' "$HELPER_FILE" 2>/dev/null)
    [ "$owner" = "root" ] || die "The helper is owned by '$owner', not root."

    echo
    success "Fan control is installed."
    echo "  Your existing installation was not touched."
    echo "  Open Tools > Fan Control to use it; no restart is needed."
    exit 0
fi

# ---- preflight --------------------------------------------------------------

# Which processes are actually a running KSysSupervisor.
#
# Matching "KSysSupervisor.py" against full command lines catches far too much:
# an editor with the file open, a grep for it, or the very shell running this
# script all contain that text. Matched against the interpreter's arguments
# and filtered to real python processes instead, with this script's own
# process tree excluded.
running_instances() {
    local pid cmd out=""
    for pid in $(pgrep -f "KSysSupervisor\.py" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        [ "$pid" = "$PPID" ] && continue
        cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || continue
        # A real instance is a python interpreter whose script argument is
        # KSysSupervisor.py - not a shell that merely mentions it.
        case "$cmd" in
            *python*[\ /]KSysSupervisor.py*) out="$out $pid" ;;
        esac
    done
    echo "${out# }"
}


# Overwriting the files of a running instance leaves it running from deleted
# inodes - and if it is holding fans under manual control, its helper is
# replaced underneath it.
RUNNING=$(running_instances)
if [ -n "$RUNNING" ] && [ "$ASSUME_YES" -eq 0 ]; then
    warn "KSysSupervisor is running (pid: ${RUNNING% })."
    echo "         Close it before installing, or it will keep running the old"
    echo "         version until you do."
    if [ -t 0 ]; then
        printf "         Continue anyway? [y/N] "
        read -r answer
        case "$answer" in [yY]*) ;; *) info "Nothing was installed."; exit 1 ;; esac
    fi
fi

command -v python3 >/dev/null 2>&1 || die "python3 is not installed or not in PATH."

for required in KSysSupervisor.py ksyssupervisor requirements.txt; do
    [ -e "$SOURCE_DIR/$required" ] || die "Missing $required - run this from a full checkout."
done

# ---- application files ------------------------------------------------------

info "Creating directories..."
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR" "$PIXMAP_DIR" \
    || die "Could not create the installation directories."

# The old name's installation would otherwise sit alongside this one, with its
# own launcher and menu entry, and the user would have two of everything.
for legacy in "$INSTALL_DIR/../ksysmonitor" "$BIN_DIR/ksysmonitor" \
              "$DESKTOP_DIR/ksysmonitor.desktop" \
              "$ICON_DIR/ksysmonitor.svg" "$PIXMAP_DIR/ksysmonitor.svg"; do
    if [ -e "$legacy" ]; then
        rm -rf "$legacy" && info "Removed the old KSysMonitor copy at $legacy"
    fi
done

info "Copying application files to $INSTALL_DIR..."

# Copied into a staging directory first and swapped in only once every copy
# has succeeded. Deleting the old files first meant a copy that failed halfway
# - a full disk, an update interrupted - left neither version working.
STAGE="$INSTALL_DIR/.new"
rm -rf -- "$STAGE"
mkdir -p "$STAGE" || die "Could not create $STAGE"
stage_failed() { rm -rf -- "$STAGE"; die "$1 - the existing installation was left as it was."; }

cp "$SOURCE_DIR/KSysSupervisor.py"   "$STAGE/" || stage_failed "Could not copy KSysSupervisor.py"
cp -r "$SOURCE_DIR/ksyssupervisor"   "$STAGE/" || stage_failed "Could not copy the package"
cp "$SOURCE_DIR/requirements.txt"    "$STAGE/" || stage_failed "Could not copy requirements.txt"
mkdir -p "$STAGE/Icons"
cp "$SOURCE_DIR/Icons/iconblue.svg"  "$STAGE/Icons/" || stage_failed "Could not copy the icon"
for doc in LICENSE README.md CHANGELOG.md uninstall.sh; do
    [ -f "$SOURCE_DIR/$doc" ] && { cp "$SOURCE_DIR/$doc" "$STAGE/" || stage_failed "Could not copy $doc"; }
done

# The fan helper and its policy travel with the app, as they do in the
# AppImage, so Tools > Fan Control can install the helper itself - one
# password prompt, from any install, without running this script again.
# Copied, not installed: nothing here runs as root until the user says so.
mkdir -p "$STAGE/helpers" "$STAGE/packaging"
cp "$SOURCE_DIR/helpers/ksyssupervisor-fanhelper" "$STAGE/helpers/" \
    || stage_failed "Could not copy the fan helper"
cp "$SOURCE_DIR/packaging/org.ksyssupervisor.fancontrol.policy" \
    "$STAGE/packaging/" || stage_failed "Could not copy the fan control policy"

# Bytecode copied from the source tree belongs to that checkout, not to this
# install, and a .pyc that outlives the .py it came from can shadow an edit -
# which is how "I installed the new version but it behaves like the old one"
# happens. Python regenerates what it needs from the .py files.
#
# -prune keeps find from descending into a directory it is about to delete,
# which otherwise makes it fail partway through and leave some behind.
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf -- {} + 2>/dev/null
find "$STAGE" -name '*.pyc' -type f -delete 2>/dev/null

# Everything except the venv and the staging directory goes, including the
# package directory itself: a module deleted from the source must not stay
# installed and importable forever.
find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .new \
    -exec rm -rf -- {} + 2>/dev/null
for item in "$STAGE"/* "$STAGE"/.[!.]*; do
    [ -e "$item" ] || continue
    mv -- "$item" "$INSTALL_DIR/" || die "Could not move $(basename "$item") into place."
done
rmdir -- "$STAGE" 2>/dev/null
true

# ---- virtual environment ----------------------------------------------------

VENV_DIR="$INSTALL_DIR/venv"
[ "$REBUILD_VENV" -eq 1 ] && { info "Rebuilding the virtual environment..."; rm -rf "$VENV_DIR"; }

if [ ! -x "$VENV_DIR/bin/python3" ]; then
    info "Creating the Python virtual environment..."
    rm -rf "$VENV_DIR"
    if ! python3 -m venv --system-site-packages "$VENV_DIR" 2>/dev/null; then
        warn "Falling back to a standard venv (no system site packages)."
        python3 -m venv "$VENV_DIR" \
            || die "Could not create the venv. Install python3-venv (or python-virtualenv) first."
    fi
fi

info "Checking dependencies (PyQt6, psutil)..."
# Run on every install, not only when an import fails: a new release may raise
# a minimum version, and an update must bring an existing venv up to it. With
# everything already satisfied pip changes nothing and needs no network.
if ! "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet 2>/dev/null; then
    if "$VENV_DIR/bin/python3" -c "import PyQt6, psutil" 2>/dev/null; then
        warn "Could not check the dependencies against requirements.txt (offline?);"
        echo "         the ones already installed are being kept."
    else
        info "Installing dependencies..."
        "$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null
        "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet \
            || die "Could not install the Python dependencies."
    fi
fi
"$VENV_DIR/bin/python3" -c "import PyQt6, psutil" 2>/dev/null \
    || die "PyQt6 and psutil are still not importable after installation."

# ---- launcher ---------------------------------------------------------------

LAUNCHER_FILE="$BIN_DIR/ksyssupervisor"
info "Creating the launcher at $LAUNCHER_FILE..."
cat > "$LAUNCHER_FILE" <<EOF || die "Could not write the launcher."
#!/usr/bin/env bash
# KSysSupervisor launcher wrapper
APP_DIR="$INSTALL_DIR"
exec "\$APP_DIR/venv/bin/python3" "\$APP_DIR/KSysSupervisor.py" "\$@"
EOF
chmod +x "$LAUNCHER_FILE"

# ---- desktop entry and icons ------------------------------------------------

info "Installing the icon and desktop entry..."
ICON_SRC="$SOURCE_DIR/Icons/iconblue.svg"
if [ -f "$ICON_SRC" ]; then
    cp "$ICON_SRC" "$ICON_DIR/ksyssupervisor.svg"
    cp "$ICON_SRC" "$PIXMAP_DIR/ksyssupervisor.svg"
else
    warn "Icon not found at $ICON_SRC; the launcher entry will have no icon."
fi

DESKTOP_FILE="$DESKTOP_DIR/ksyssupervisor.desktop"
cat > "$DESKTOP_FILE" <<EOF || die "Could not write the desktop entry."
[Desktop Entry]
Type=Application
Name=KSysSupervisor
GenericName=Hardware Monitor
Comment=HWMonitor-style hardware and sensor monitor for Linux
Exec=$LAUNCHER_FILE
Icon=ksyssupervisor
Terminal=false
Categories=System;Monitor;Qt;
StartupNotify=true
StartupWMClass=ksyssupervisor
Keywords=system;monitor;sensors;hardware;cpu;gpu;temperature;voltage;fan;
EOF
chmod 644 "$DESKTOP_FILE"

KMENU_FILE="${XDG_CONFIG_HOME:-${USER_HOME:-$HOME}/.config}/menus/applications-kmenuedit.menu"
if [ -f "$KMENU_FILE" ] && grep -q "ksyssupervisor.desktop" "$KMENU_FILE" 2>/dev/null; then
    sed -i '/<Filename>ksyssupervisor\.desktop<\/Filename>/d' "$KMENU_FILE" 2>/dev/null
fi

# ---- fan control helper -----------------------------------------------------

if [ "$INSTALL_MODE" = "system" ]; then
    install_fan_control || true
else
    info "Fan control's helper is not installed by a per-user install."
    echo "         It runs as root, so it must live somewhere you cannot rewrite."
    echo "         Open Tools > Fan Control and it offers to install just the"
    echo "         helper, with one password prompt - this installation stays"
    echo "         where it is. 'sudo ./install.sh --fan-control' does the same."
fi

# ---- caches -----------------------------------------------------------------

info "Updating the desktop and icon caches..."
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_DIR" 2>/dev/null
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$(dirname "$ICON_DIR")" 2>/dev/null
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    kbuildsycoca6 --noincremental >/dev/null 2>&1
elif command -v kbuildsycoca5 >/dev/null 2>&1; then
    kbuildsycoca5 --noincremental >/dev/null 2>&1
fi

# ---- verify -----------------------------------------------------------------

PROBLEMS=()
[ -f "$INSTALL_DIR/KSysSupervisor.py" ]   || PROBLEMS+=("$INSTALL_DIR/KSysSupervisor.py is missing")
[ -d "$INSTALL_DIR/ksyssupervisor" ]      || PROBLEMS+=("$INSTALL_DIR/ksyssupervisor is missing")
[ -x "$LAUNCHER_FILE" ]                || PROBLEMS+=("$LAUNCHER_FILE is not executable")
[ -f "$DESKTOP_FILE" ]                 || PROBLEMS+=("$DESKTOP_FILE is missing")
[ -x "$VENV_DIR/bin/python3" ]         || PROBLEMS+=("the venv has no python3")

# Importing the package is what actually proves the install works; a copied
# tree that cannot be imported would otherwise be reported as a success.
if ! "$VENV_DIR/bin/python3" -c "
import sys; sys.path.insert(0, '$INSTALL_DIR')
import ksyssupervisor; print('    version', ksyssupervisor.APP_VERSION)
" 2>/dev/null; then
    PROBLEMS+=("the installed package cannot be imported")
fi

if [ "$FAN_CONTROL" = "yes" ]; then
    [ -x "$HELPER_FILE" ] || PROBLEMS+=("$HELPER_FILE is not executable")
    [ -f "$POLICY_FILE" ] || PROBLEMS+=("$POLICY_FILE is missing")
    owner=$(stat -c '%U' "$HELPER_FILE" 2>/dev/null)
    [ "$owner" = "root" ] || PROBLEMS+=("the fan helper is owned by '$owner', not root")
fi

if [ ${#PROBLEMS[@]} -gt 0 ]; then
    error "The installation finished with problems:"
    printf '    - %s\n' "${PROBLEMS[@]}"
    exit 1
fi

echo
success "KSysSupervisor is installed."
echo -e "  ${BOLD}Run:${NC}       ksyssupervisor"
echo -e "  ${BOLD}App menu:${NC}  search for 'KSysSupervisor'"

if [ "$FAN_CONTROL" = "yes" ]; then
    echo -e "  ${BOLD}Fan control:${NC} enabled, under Tools > Fan Control."
    echo "               Fans go back to automatic when KSysSupervisor exits,"
    echo "               including if it crashes, unless you ask otherwise."
fi

if [ "$INSTALL_MODE" = "user" ] && [[ ":${PATH:-}:" != *":$BIN_DIR:"* ]]; then
    echo
    warn "'$BIN_DIR' is not in your PATH."
    echo "         Add this to ~/.bashrc or ~/.zshrc to run it by name:"
    echo -e "         ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${NC}"
fi
