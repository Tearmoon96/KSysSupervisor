#!/usr/bin/env bash
#
# KSysSupervisor uninstaller.
#
# Removes user-level and/or system-wide installations. Two rules shape this
# script, both learned from the version it replaces:
#
#   1. It never reports success it has not verified. The old script printed
#      "uninstallation completed" and exited 0 after removing nothing at all,
#      which is the worst possible outcome: the user believes the machine is
#      clean when it is not. Every removal is checked afterwards, and anything
#      still standing is an error.
#
#   2. It keeps going when one removal fails. Stopping at the first error left
#      a half-removed install with no report of what remained. Failures are
#      collected and listed at the end instead.

set -uo pipefail   # deliberately not -e: see rule 2 above

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'
YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}${BOLD}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}${BOLD}[SUCCESS]${NC} $1"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARNING]${NC} $1"; }
error()   { echo -e "${RED}${BOLD}[ERROR]${NC} $1"; }

FAILURES=()
REMOVED=0
DRY_RUN=0

# Remove one path and confirm it is gone.
#
# A path that was never there is not a failure - uninstalling something only
# partly installed is a normal thing to do. A path that survives the rm is,
# because that is precisely the case the old script called success.
drop() {
    local target="$1"
    [ -e "$target" ] || [ -L "$target" ] || return 0

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "         would remove  $target"
        return 0
    fi

    rm -rf -- "$target" 2>/dev/null
    if [ -e "$target" ] || [ -L "$target" ]; then
        FAILURES+=("$target")
        return 1
    fi
    echo "         removed  $target"
    REMOVED=$((REMOVED + 1))
}

MODE="auto"
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --system) MODE="system" ;;
        --user)   MODE="user" ;;
        --all)    MODE="all" ;;
        --purge)  PURGE=1 ;;
        --fan-control) MODE="fan-control" ;;
        --dry-run) DRY_RUN=1 ;;
        --help|-h)
            cat <<'USAGE'
Usage: ./uninstall.sh [OPTIONS]

Options:
  --user      Remove this user's installation
  --system    Remove the system-wide installation (needs root)
  --all       Remove both
  --fan-control  Remove ONLY the fan control helper and its polkit policy,
              leaving the application itself installed
  --purge     Also delete settings (~/.config/KSysSupervisor), including saved
              fan names and window geometry
  --dry-run   List what would be removed, change nothing
  --help      Show this message

With no option, removes whatever is installed, re-running itself through
sudo for the system part if that is needed and possible.
USAGE
            exit 0
            ;;
        *) warn "Unknown option '$arg', ignoring." ;;
    esac
done

# Whose files count as "the user's".
#
# Under sudo or pkexec, $HOME may already be root's, so trusting it would look
# for the user's install in /root and quietly find nothing - reporting a clean
# removal while the real files stayed put. The invoking user is named in
# SUDO_USER / PKEXEC_UID, so ask for that home explicitly.
TARGET_USER="$(id -un)"
if [ "$(id -u)" -eq 0 ]; then
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        TARGET_USER="$SUDO_USER"
    elif [ -n "${PKEXEC_UID:-}" ]; then
        TARGET_USER="$(getent passwd "$PKEXEC_UID" | cut -d: -f1)"
    fi
fi

# For the user running this, $HOME - the same one install.sh installed under;
# the passwd entry is only for someone else's files, reached through sudo.
if [ "$TARGET_USER" = "$(id -un)" ] && [ -n "${HOME:-}" ]; then
    USER_HOME="$HOME"
else
    USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    [ -n "$USER_HOME" ] || USER_HOME="/home/$TARGET_USER"
fi

# XDG_* in the environment belong to whoever ran sudo, so they only apply when
# that is also the user whose files we are removing.
if [ "$TARGET_USER" = "$(id -un)" ]; then
    DATA_HOME="${XDG_DATA_HOME:-$USER_HOME/.local/share}"
    CONFIG_HOME="${XDG_CONFIG_HOME:-$USER_HOME/.config}"
else
    DATA_HOME="$USER_HOME/.local/share"
    CONFIG_HOME="$USER_HOME/.config"
fi

# ---- detection --------------------------------------------------------------

SYSTEM_PATHS=(
    "/opt/ksyssupervisor"
    "/opt/ksysmonitor"                          # pre-release name
    "/usr/local/share/ksyssupervisor"
    "/usr/local/bin/ksyssupervisor"
    "/usr/bin/ksyssupervisor"
    "/usr/share/applications/ksyssupervisor.desktop"
    "/usr/local/share/applications/ksyssupervisor.desktop"
    "/usr/share/icons/hicolor/scalable/apps/ksyssupervisor.svg"
    "/usr/share/pixmaps/ksyssupervisor.svg"
    # Fan control. The polkit action has to go with the helper it authorises:
    # left behind, it would keep naming a path that no longer exists but that
    # something else could later occupy.
    "/usr/share/polkit-1/actions/org.ksyssupervisor.fancontrol.policy"
    "/usr/local/lib/ksyssupervisor"
    # The pre-release name. Listed so a plain uninstall cannot leave a root-owned
    # helper behind - still authorised by its own polkit action - just because
    # it predates the rename.
    "/usr/share/polkit-1/actions/org.ksysmonitor.fancontrol.policy"
    "/usr/local/lib/ksysmonitor"
    "/usr/local/bin/ksysmonitor"
    "/usr/share/applications/ksysmonitor.desktop"
    "/usr/share/icons/hicolor/scalable/apps/ksysmonitor.svg"
    "/usr/share/pixmaps/ksysmonitor.svg"
)

# The two files fan control needs, which can be installed and removed on their
# own: the application does not have to be system-wide for them to work.
FAN_PATHS=(
    "/usr/share/polkit-1/actions/org.ksyssupervisor.fancontrol.policy"
    "/usr/local/lib/ksyssupervisor"
    # The pre-release name. Removed here too, so uninstalling cannot leave a
    # root-owned helper behind just because it predates the rename.
    "/usr/share/polkit-1/actions/org.ksysmonitor.fancontrol.policy"
    "/usr/local/lib/ksysmonitor"
)

USER_PATHS=(
    "$DATA_HOME/ksyssupervisor"
    "$DATA_HOME/ksysmonitor"                    # pre-release name
    "$USER_HOME/.local/bin/ksysmonitor"
    "$DATA_HOME/applications/ksysmonitor.desktop"
    "$USER_HOME/.local/bin/ksyssupervisor"
    "$DATA_HOME/applications/ksyssupervisor.desktop"
    "$DATA_HOME/icons/hicolor/scalable/apps/ksyssupervisor.svg"
    "$DATA_HOME/pixmaps/ksyssupervisor.svg"
)

anything_present() {
    local path
    for path in "$@"; do
        [ -e "$path" ] || [ -L "$path" ] && return 0
    done
    return 1
}

# ---- safety checks ----------------------------------------------------------

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

check_running() {
    # Removing the files under a running instance leaves it working from
    # deleted inodes until it exits, which makes "did the uninstall work?"
    # unanswerable. Worth one warning.
    local pids
    pids=$(running_instances)
    [ -n "$pids" ] || return 0

    warn "KSysSupervisor is running (pid: ${pids% })."
    echo "         Close it first, so it is not still holding fans under manual"
    echo "         control when its helper is removed."
    if [ -t 0 ]; then
        printf "         Continue anyway? [y/N] "
        read -r answer
        case "$answer" in [yY]*) ;; *) info "Nothing was removed."; exit 1 ;; esac
    fi
}

check_manual_fans() {
    # The helper restores fans when it exits, so a fan left in manual mode means
    # a helper still alive or a session that ended with "keep". Either way the
    # board is not regulating that fan, and deleting the tool that could put it
    # back without saying so would be unhelpful.
    local found=()
    local pwm chip enable
    for pwm in /sys/class/hwmon/hwmon*/pwm[0-9]_enable; do
        [ -r "$pwm" ] || continue
        enable=$(cat "$pwm" 2>/dev/null) || continue
        [ "$enable" = "1" ] || continue
        chip=$(cat "$(dirname "$pwm")/name" 2>/dev/null)
        found+=("${chip:-unknown}: $(basename "${pwm%_enable}")")
    done
    [ ${#found[@]} -gt 0 ] || return 0

    warn "${#found[@]} fan channel(s) are set to manual control right now:"
    printf '         - %s\n' "${found[@]}"
    echo "         Reboot, or set them back with KSysSupervisor before removing it."
}

# ---- removal ----------------------------------------------------------------

uninstall_user() {
    info "Removing ${TARGET_USER}'s KSysSupervisor files (in $USER_HOME)..."
    local path
    for path in "${USER_PATHS[@]}"; do drop "$path"; done

    if [ "$PURGE" -eq 1 ]; then
        # Only on request: these are the user's saved fan names and window
        # geometry, which they may well want back on the next install.
        drop "$CONFIG_HOME/KSysSupervisor"
        drop "$CONFIG_HOME/KSysSupervisor.conf"
    elif [ -e "$CONFIG_HOME/KSysSupervisor" ] || [ -e "$CONFIG_HOME/KSysSupervisor.conf" ]; then
        info "Settings kept at $CONFIG_HOME/KSysSupervisor (use --purge to delete)."
    fi

    if [ "$DRY_RUN" -eq 0 ]; then
        command -v update-desktop-database >/dev/null 2>&1 && \
            update-desktop-database "$DATA_HOME/applications" 2>/dev/null
        command -v gtk-update-icon-cache >/dev/null 2>&1 && \
            gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" 2>/dev/null
    fi
    return 0
}

uninstall_system() {
    if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
        error "Removing the system-wide installation needs root."
        echo "    sudo ./uninstall.sh --system"
        return 1
    fi
    info "Removing the system-wide KSysSupervisor files..."
    local path
    for path in "${SYSTEM_PATHS[@]}"; do drop "$path"; done

    if [ "$DRY_RUN" -eq 0 ]; then
        command -v update-desktop-database >/dev/null 2>&1 && \
            update-desktop-database /usr/share/applications 2>/dev/null
        command -v gtk-update-icon-cache >/dev/null 2>&1 && \
            gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null
    fi
    return 0
}

uninstall_fan_control() {
    if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
        error "Removing the fan control helper needs root."
        echo "    sudo ./uninstall.sh --fan-control"
        return 1
    fi
    info "Removing the fan control helper and polkit policy..."
    local path
    for path in "${FAN_PATHS[@]}"; do drop "$path"; done
    return 0
}

refresh_kde_cache() {
    [ "$DRY_RUN" -eq 1 ] && return 0
    if command -v kbuildsycoca6 >/dev/null 2>&1; then
        kbuildsycoca6 --noincremental >/dev/null 2>&1
    elif command -v kbuildsycoca5 >/dev/null 2>&1; then
        kbuildsycoca5 --noincremental >/dev/null 2>&1
    fi
    return 0
}

# ---- main -------------------------------------------------------------------

check_running
check_manual_fans

HAVE_USER=0;   anything_present "${USER_PATHS[@]}"   && HAVE_USER=1
HAVE_SYSTEM=0; anything_present "${SYSTEM_PATHS[@]}" && HAVE_SYSTEM=1

if [ "$MODE" = "auto" ]; then
    if [ "$HAVE_USER" -eq 0 ] && [ "$HAVE_SYSTEM" -eq 0 ]; then
        info "KSysSupervisor does not appear to be installed. Nothing to do."
        exit 0
    fi

    [ "$HAVE_USER" -eq 1 ] && uninstall_user

    if [ "$HAVE_SYSTEM" -eq 1 ] && [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
        # The old script stopped here with a note and exited 0, which read as
        # "done" when nothing had been removed. Escalate instead, and if that
        # is not possible, fail loudly.
        info "A system-wide installation is present; it needs root to remove."
        if command -v sudo >/dev/null 2>&1; then
            echo
            if sudo -- "$(realpath "$0")" --system ${PURGE:+}; then
                refresh_kde_cache
                exit 0
            fi
            error "The system-wide removal did not complete."
            exit 1
        fi
        error "Cannot remove the system-wide installation: no sudo available."
        echo "    Run this as root:  ./uninstall.sh --system"
        exit 1
    fi

    [ "$HAVE_SYSTEM" -eq 1 ] && { uninstall_system || exit 1; }
else
    case "$MODE" in
        user)   uninstall_user ;;
        system) uninstall_system || exit 1 ;;
        all)    uninstall_user; uninstall_system || exit 1 ;;
        fan-control) uninstall_fan_control || exit 1 ;;
    esac
fi

refresh_kde_cache

# ---- verify -----------------------------------------------------------------

if [ "$DRY_RUN" -eq 1 ]; then
    info "Dry run: nothing was changed."
    exit 0
fi

# The point of the whole script: check, do not assume.
LEFTOVER=()
case "$MODE" in
    user)        CHECK=("${USER_PATHS[@]}") ;;
    system)      CHECK=("${SYSTEM_PATHS[@]}") ;;
    fan-control) CHECK=("${FAN_PATHS[@]}") ;;
    *)           CHECK=("${USER_PATHS[@]}" "${SYSTEM_PATHS[@]}") ;;
esac
for path in "${CHECK[@]}"; do
    [ -e "$path" ] || [ -L "$path" ] && LEFTOVER+=("$path")
done

if [ ${#FAILURES[@]} -gt 0 ] || [ ${#LEFTOVER[@]} -gt 0 ]; then
    error "KSysSupervisor was not fully removed. Still present:"
    printf '    %s\n' "${LEFTOVER[@]}" | sort -u
    echo
    if [ "$(id -u)" -ne 0 ]; then
        echo "    Most likely these need root:  sudo ./uninstall.sh --all"
    fi
    exit 1
fi

if [ "$REMOVED" -eq 0 ]; then
    info "Nothing was installed. Nothing to do."
else
    success "KSysSupervisor removed ($REMOVED item(s))."
fi
