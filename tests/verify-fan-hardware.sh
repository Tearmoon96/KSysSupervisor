#!/usr/bin/env bash
#
# End-to-end check of fan control against the real hardware.
#
# Run as root. Everything it does is undone: the state of every channel is
# recorded before anything is written, and a trap restores it on any exit -
# including a failed assertion, a Ctrl-C, or a SIGTERM. If this script leaves
# a fan under manual control, that is itself a bug.
#
# It drives the installed helper directly over a pipe, exactly the way the GUI
# does, so what it exercises is the real protocol and not a reimplementation.

set -uo pipefail

HELPER="${HELPER:-/usr/local/lib/ksyssupervisor/ksyssupervisor-fanhelper}"
HOLD="${HOLD:-3}"          # seconds a channel stays at a test speed

# Normally the real sysfs. Pointed at a fake tree by the harness's own tests,
# which is the only way to catch a bug in this script without spending a
# password prompt and a few minutes of somebody's fans to find it.
HWMON_ROOT="${HWMON_ROOT:-/sys/class/hwmon}"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

PASS=0; FAIL=0; SKIP=0
FAILED_NAMES=()

pass() { echo -e "  ${GREEN}PASS${NC}  $1"; PASS=$((PASS+1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); }
skip() { echo -e "  ${YELLOW}SKIP${NC}  $1"; SKIP=$((SKIP+1)); }
head1() { echo; echo -e "${BOLD}$1${NC}"; }
note() { echo -e "  ${BLUE}·${NC}     $1"; }

# Signal the helper processes, and only them.
#
# "pkill -f $HELPER" matches any command line containing that path - this
# script's own, and the wrapper that launched it. A script that kills itself
# reports a failure that never happened and leaves the fans wherever they were,
# which is how the previous run ended.
kill_helpers() {
    local sig="$1" pid found=0
    for pid in $(pgrep -x python3 2>/dev/null; pgrep -x ksyssupervisor-fanhelper 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        case "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" in
            *ksyssupervisor-fanhelper*) kill "-$sig" "$pid" 2>/dev/null; found=1 ;;
        esac
    done
    return $((1 - found))
}

if [ "$HWMON_ROOT" = "/sys/class/hwmon" ] && [ "$(id -u)" -ne 0 ]; then
    echo "This must run as root (or set HWMON_ROOT to a fake tree)."
    exit 1
fi
[ -x "$HELPER" ] || { echo "Helper not found at $HELPER"; exit 1; }
# Taken from the helper under test rather than written in here, so the check
# cannot fall behind the next protocol bump and fail every handshake.
PROTO=$(sed -n 's/^PROTOCOL_VERSION = \([0-9][0-9]*\)$/\1/p' "$HELPER")
[ -n "$PROTO" ] || { echo "No PROTOCOL_VERSION in $HELPER"; exit 1; }

# ---- baseline and restoration ----------------------------------------------

declare -A SAVED_PWM SAVED_ENABLE
CHANNELS=()

for dir in "$HWMON_ROOT"/hwmon*; do
    name=$(cat "$dir/name" 2>/dev/null) || continue
    for pwm in "$dir"/pwm[0-9]; do
        [ -e "$pwm" ] || continue
        idx=$(basename "$pwm" | tr -d 'pwm')
        key="$dir:$idx"
        CHANNELS+=("$key")
        SAVED_PWM[$key]=$(cat "$pwm" 2>/dev/null)
        SAVED_ENABLE[$key]=$(cat "${pwm}_enable" 2>/dev/null)
    done
done

# The whole point of the exercise is that fans end up back where they started,
# so this runs no matter how the script ends.
restore_everything() {
    local key dir idx
    for key in "${CHANNELS[@]}"; do
        dir="${key%:*}"; idx="${key##*:}"
        # pwm before enable: switching the mode back hands the channel to the
        # driver, which then owns the duty anyway.
        [ -n "${SAVED_PWM[$key]:-}" ] && \
            echo "${SAVED_PWM[$key]}" > "$dir/pwm$idx" 2>/dev/null
        [ -n "${SAVED_ENABLE[$key]:-}" ] && \
            echo "${SAVED_ENABLE[$key]}" > "$dir/pwm${idx}_enable" 2>/dev/null
    done
}
trap 'echo; echo "Restoring every channel..."; restore_everything' EXIT INT TERM

echo -e "${BOLD}KSysSupervisor fan control - hardware verification${NC}"
echo "Helper: $HELPER"
echo "Channels found: ${#CHANNELS[@]}"

# ---- helper session ---------------------------------------------------------

# Talk to one long-lived helper over a pair of FIFOs, which is how the GUI
# holds it open across many commands.
TMPDIR_RUN=$(mktemp -d)

# A function rather than a straight line, because section 4 ends by killing the
# helper on purpose and anything after it needs a live one again. Fresh FIFOs
# each time: the old pair still has a dead writer at the far end.
SESSION=0
start_helper_session() {
    exec 3>&- 2>/dev/null
    exec 4<&- 2>/dev/null
    SESSION=$((SESSION+1))
    IN="$TMPDIR_RUN/in.$SESSION"; OUT="$TMPDIR_RUN/out.$SESSION"
    mkfifo "$IN" "$OUT"
    "$HELPER" < "$IN" > "$OUT" 2>>"$TMPDIR_RUN/err" &
    HELPER_PID=$!
    exec 3>"$IN" 4<"$OUT"
}
start_helper_session

cleanup_helper() {
    exec 3>&- 2>/dev/null
    exec 4<&- 2>/dev/null
    kill "$HELPER_PID" 2>/dev/null
    wait "$HELPER_PID" 2>/dev/null
    rm -rf "$TMPDIR_RUN"
}
trap 'echo; echo "Restoring every channel..."; cleanup_helper; restore_everything' EXIT INT TERM

# One command in, one reply out, with a timeout so a wedged helper cannot hang
# a script that is holding fans in manual mode.
say() {
    local reply
    if ! echo "$1" >&3 2>/dev/null; then
        echo "DEAD"
        return
    fi
    if read -r -t 10 reply <&4; then echo "$reply"; else echo "TIMEOUT"; fi
}

# What the helper said on the way out. Captured all along but never shown
# before, which left a mid-run death with no explanation at all.
helper_stderr() {
    if [ -s "$TMPDIR_RUN/err" ]; then
        echo "  helper stderr:"
        sed 's/^/    /' "$TMPDIR_RUN/err"
    else
        echo "  helper stderr: (empty)"
    fi
    if kill -0 "$HELPER_PID" 2>/dev/null; then
        echo "  helper is still running (pid $HELPER_PID)"
    else
        wait "$HELPER_PID" 2>/dev/null
        echo "  helper has exited (status $?)"
    fi
}

head1 "1. Handshake"
reply=$(say "HELLO $PROTO")
[ "$reply" = "READY $PROTO" ] && pass "helper answered: $reply" || fail "handshake: got '$reply'"

head1 "2. Rejecting what it should reject"
first_hwmon="${CHANNELS[0]%:*}"
for bad in "CLAIM /etc 1|outside hwmon" \
           "CLAIM $HWMON_ROOT/../../etc 1|traversal" \
           "CLAIM $first_hwmon 999|absurd index" \
           "CLAIM $first_hwmon x|non-numeric index" \
           "NONSENSE|unknown command"; do
    cmd="${bad%|*}"; what="${bad#*|}"
    reply=$(say "$cmd")
    case "$reply" in
        ERR*) pass "refused $what" ;;
        *)    fail "$what was NOT refused: '$reply'" ;;
    esac
done

reply=$(say "SET $first_hwmon 1 128")
case "$reply" in
    ERR*) pass "refused SET without a prior CLAIM" ;;
    *)    fail "SET without CLAIM was accepted: '$reply'" ;;
esac

# ---- per-channel exercise ---------------------------------------------------

head1 "3. Each channel: claim, drive, read back, restore"

for key in "${CHANNELS[@]}"; do
    dir="${key%:*}"; idx="${key##*:}"
    chip=$(cat "$dir/name")
    label="$chip pwm$idx"
    rpm_file="$dir/fan${idx}_input"

    echo
    echo -e "${BOLD}--- $label${NC} (was pwm=${SAVED_PWM[$key]} enable=${SAVED_ENABLE[$key]})"

    reply=$(say "CLAIM $dir $idx")
    case "$reply" in
        OK*)
            set -- $reply
            pass "claimed (enable=$2 pwm=$3 max=${4:-?})"
            claimed_max="${4:-255}"
            ;;
        TIMEOUT|DEAD)
            fail "$label: claim failed: '$reply'"
            helper_stderr
            continue ;;
        *) fail "$label: claim failed: '$reply'"; continue ;;
    esac

    # Sanity: what CLAIM reports must match what is on disk, or the restore
    # would put back the wrong number. Compared against a reading taken now,
    # not against the baseline: a channel under its own curve moves on its own
    # between the two, and that drift is the driver working, not a mismatch.
    live_pwm=$(cat "$dir/pwm$idx")
    live_en=$(cat "$dir/pwm${idx}_enable" 2>/dev/null)
    if [ "$3" = "$live_pwm" ]; then
        pass "reported pwm matches sysfs"
    elif [ "$live_en" != "1" ]; then
        # The channel is running its own curve, so it moves between the claim
        # and this reading. That drift is the driver regulating, not a
        # disagreement - only a channel already in manual mode holds still
        # enough for this to mean anything.
        note "claim said $3, now $live_pwm - channel is self-regulating (enable=$live_en)"
        pass "claim read a live value from an automatic channel"
    else
        fail "$label: claim said pwm=$3, sysfs says $live_pwm"
    fi

    before_rpm=$(cat "$rpm_file" 2>/dev/null || echo "-")

    # Drive it high, then low. High first so a fan that was stopped has a
    # chance to spin up before the low reading is taken.
    for target in $(( claimed_max * 90 / 100 )) $(( claimed_max * 45 / 100 )); do
        reply=$(say "SET $dir $idx $target")
        case "$reply" in
            OK*)
                set -- $reply
                actual=$2
                on_disk=$(cat "$dir/pwm$idx")
                mode=$(cat "$dir/pwm${idx}_enable" 2>/dev/null)
                if [ "$on_disk" = "$actual" ]; then
                    pass "set to $target -> driver kept $actual (enable=$mode)"
                else
                    fail "$label: helper said $actual but sysfs holds $on_disk"
                fi
                sleep "$HOLD"
                now_rpm=$(cat "$rpm_file" 2>/dev/null || echo "-")
                note "rpm: $before_rpm -> $now_rpm"
                ;;
            ERR*)
                # This is the case the readback exists to catch, and on some
                # AMD cards it is the correct answer rather than a defect.
                note "refused: ${reply#ERR }"
                skip "$label at $target: driver would not take it"
                ;;
            *) fail "$label: unexpected reply '$reply'" ;;
        esac
    done

    # What "manual" is numbered as: 1 on every driver, dell-smm included
    # (dell_smm_write() maps 1 to "BIOS control off", 2 to "on").
    manual_value=1

    reply=$(say "AUTO $dir $idx")
    if [ "$reply" = "OK" ]; then
        back_pwm=$(cat "$dir/pwm$idx")
        back_en=$(cat "$dir/pwm${idx}_enable" 2>/dev/null)
        # AUTO means "under the driver's regulation", not "back to what CLAIM
        # found". The two differ for a channel that was already manual when it
        # was claimed - a fan a previous session deliberately left pinned -
        # where restoring what was found would put it straight back into
        # manual, which is the state the button exists to leave.
        if [ "$back_en" = "${SAVED_ENABLE[$key]}" ]; then
            pass "restored to automatic (enable=$back_en, pwm=$back_pwm)"
        elif [ "${SAVED_ENABLE[$key]}" = "$manual_value" ]; then
            if [ "$back_en" != "$manual_value" ]; then
                pass "was claimed in manual, handed back to automatic (enable=$back_en)"
            else
                fail "$label: still manual after AUTO (enable=$back_en)"
            fi
        else
            fail "$label: enable came back as $back_en, expected ${SAVED_ENABLE[$key]}"
        fi
    else
        fail "$label: AUTO failed: '$reply'"
        case "$reply" in TIMEOUT|DEAD) helper_stderr ;; esac
    fi
done

# ---- the safety guarantee ---------------------------------------------------

head1 "4. Crash safety: a killed client must release the fans"

# Pick a channel that is currently regulated, so "went back to automatic" is a
# visible change rather than a no-op.
VICTIM=""
for key in "${CHANNELS[@]}"; do
    dir="${key%:*}"; idx="${key##*:}"
    [ -e "$dir/pwm${idx}_enable" ] || continue
    [ "${SAVED_ENABLE[$key]}" = "1" ] && continue   # already manual, proves nothing
    VICTIM="$key"; break
done

if [ -z "$VICTIM" ]; then
    skip "no automatically-regulated channel to test with"
else
    dir="${VICTIM%:*}"; idx="${VICTIM##*:}"
    chip=$(cat "$dir/name")

    # Restore the victim to a known automatic state before each scenario, so
    # one run cannot poison the next: a helper that claims a channel already
    # left at enable=1 will faithfully restore it to 1, which would look like
    # a failure while actually being correct behaviour.
    reset_victim() {
        echo "${SAVED_PWM[$VICTIM]}" > "$dir/pwm$idx" 2>/dev/null
        echo "${SAVED_ENABLE[$VICTIM]}" > "$dir/pwm${idx}_enable" 2>/dev/null
        sleep 1
    }

    for scenario in "client-SIGKILL" "client-exit" "helper-SIGTERM"; do
        echo
        echo -e "${BOLD}--- $scenario on $chip pwm$idx${NC}"
        reset_victim

        # A stand-in for the GUI: a process that owns the helper and holds the
        # pipe open. Killing *this* is what the safety design is about - the
        # helper is never the thing that dies in the scenario being modelled.
        # A stand-in for the GUI, built on FIFOs because the process
        # substitution this used before never reached the helper at all - the
        # channel stayed automatic and every scenario "passed" by doing
        # nothing, which is the least useful kind of green.
        rm -f "$TMPDIR_RUN/cin" "$TMPDIR_RUN/cout"
        mkfifo "$TMPDIR_RUN/cin" "$TMPDIR_RUN/cout"

        client_script="$TMPDIR_RUN/client.sh"
        cat > "$client_script" <<CLIENT
#!/usr/bin/env bash
"$HELPER" < "$TMPDIR_RUN/cin" > "$TMPDIR_RUN/cout" 2>/dev/null &
exec 3> "$TMPDIR_RUN/cin"
exec 4< "$TMPDIR_RUN/cout"
echo "HELLO $PROTO" >&3; read -r -t 10 a <&4
echo "CLAIM $dir $idx" >&3; read -r -t 10 b <&4
echo "SET $dir $idx 229" >&3; read -r -t 10 c <&4
echo "\$a | \$b | \$c" > "$TMPDIR_RUN/clog"
# exec, not a plain sleep: a forked child inherits fd 3 and holds the FIFO's
# write end open, so killing the client would not close the pipe and the
# helper would never see EOF. Replacing the shell keeps the fd on the very
# process this test kills.
exec sleep 120
CLIENT
        chmod +x "$client_script"

        # Not setsid: it forks, so $! would be setsid's pid and the client
        # would survive the kill with its pipe still open - no EOF, no
        # restore, and a failure reported against the helper for something the
        # test never actually did to it.
        "$client_script" &
        client_pid=$!
        sleep 3

        mid_en=$(cat "$dir/pwm${idx}_enable")
        mid_pwm=$(cat "$dir/pwm$idx")
        if [ "$mid_en" != "1" ]; then
            note "client log: $(cat "$TMPDIR_RUN/clog" 2>/dev/null || echo "(none)")"
            skip "$scenario: channel did not reach manual (enable=$mid_en); nothing to prove"
            kill -9 "$client_pid" 2>/dev/null; wait "$client_pid" 2>/dev/null
            continue
        fi
        note "client holding the channel at pwm=$mid_pwm (enable=1)"

        case "$scenario" in
            client-SIGKILL)
                # The harshest realistic case: the GUI is destroyed outright.
                # Only the kernel closing its pipes can save the fan here.
                kill -9 "$client_pid" 2>/dev/null ;;
            client-exit)
                kill -TERM "$client_pid" 2>/dev/null ;;
            helper-SIGTERM)
                # Not the GUI this time: the helper itself is asked to stop,
                # which its signal handler must treat as "restore, then go".
                kill_helpers TERM ;;
        esac
        wait "$client_pid" 2>/dev/null

        # The helper needs a moment to notice and write.
        for _ in 1 2 3 4 5; do
            sleep 1
            [ "$(cat "$dir/pwm${idx}_enable")" = "${SAVED_ENABLE[$VICTIM]}" ] && break
        done

        after_en=$(cat "$dir/pwm${idx}_enable")
        after_pwm=$(cat "$dir/pwm$idx")
        if [ "$after_en" = "${SAVED_ENABLE[$VICTIM]}" ]; then
            pass "$scenario: enable back to $after_en (pwm=$after_pwm)"
        else
            fail "$scenario: enable stuck at $after_en, expected ${SAVED_ENABLE[$VICTIM]}"
        fi
        kill_helpers TERM
        sleep 1
    done
fi

# ---- driver quirks ----------------------------------------------------------

head1 "5. How long does the GPU take to report a write?"

# This began as a question - does the card refuse decreases, or is our readback
# simply reading too soon? - and the answer was the latter: the helper now
# retries for READBACK_ATTEMPTS x READBACK_DELAY before calling a write
# refused. What is measured here is the margin that fix has left, so a future
# driver that got slower would show up as a shrinking gap rather than as a
# mysterious "the GPU cannot be turned down" report.

GPU_DIR=""
for key in "${CHANNELS[@]}"; do
    d="${key%:*}"
    [ "$(cat "$d/name")" = "amdgpu" ] && { GPU_DIR="$d"; GPU_IDX="${key##*:}"; break; }
done

if [ "$HWMON_ROOT" != "/sys/class/hwmon" ]; then
    skip "not real hardware - a plain file accepts every write, so this proves nothing"
elif [ -z "$GPU_DIR" ]; then
    skip "no amdgpu channel present"
else
    gkey="$GPU_DIR:$GPU_IDX"
    echo 1 > "$GPU_DIR/pwm${GPU_IDX}_enable" 2>/dev/null
    echo 229 > "$GPU_DIR/pwm$GPU_IDX" 2>/dev/null
    sleep 2
    note "raised to 229: reports $(cat "$GPU_DIR/pwm$GPU_IDX"), $(cat "$GPU_DIR/fan${GPU_IDX}_input") rpm"

    # Time the decrease the old code got wrong, in the same units the helper
    # retries in.
    echo 114 > "$GPU_DIR/pwm$GPU_IDX"
    waited=0
    settled=""
    for _ in $(seq 1 40); do
        v=$(cat "$GPU_DIR/pwm$GPU_IDX")
        if [ "$v" -le 120 ] 2>/dev/null; then settled="$waited"; break; fi
        sleep 0.05
        waited=$((waited + 50))
    done

    budget=250      # READBACK_ATTEMPTS(5) x READBACK_DELAY(50ms)
    if [ -z "$settled" ]; then
        fail "the card never reported the decrease within 2s"
    elif [ "$settled" -le "$budget" ]; then
        pass "card reported the decrease after ~${settled}ms (helper waits up to ${budget}ms)"
        note "margin is $((budget - settled))ms; section 3 shows the helper handling this for real"
    else
        fail "card took ${settled}ms but the helper only waits ${budget}ms - raise READBACK_ATTEMPTS"
    fi

    echo "${SAVED_PWM[$gkey]}" > "$GPU_DIR/pwm$GPU_IDX" 2>/dev/null
    echo "${SAVED_ENABLE[$gkey]}" > "$GPU_DIR/pwm${GPU_IDX}_enable" 2>/dev/null
fi

# ---- full duty --------------------------------------------------------------

head1 "6. Full duty: the value the slider deliberately will not write"

# nct6775 derives pwmN_enable from the fan-mode register and the current duty:
#
#     if (mode == 0 && pwm == 255) return off;   /* 0 */
#     return mode + 1;                           /* 1 = manual */
#
# so a channel we hold in manual stops reporting "manual" the instant the duty
# reaches its maximum. The GUI read that as the board taking the fan back, and
# greyed out a slider that was still ours - the bug this section exists for.
#
# There are two guards, and both are checked here: the app no longer writes the
# top of the range at all, and it recognises the aliasing if it meets it anyway.

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
FULL_DUTY_CHIPS=" nct6106 nct6116 nct6775 nct6776 nct6779 nct6791 nct6792 nct6793 nct6795 nct6796 nct6797 nct6798 nct6799 "

# Asked of the application rather than hard-coded here, so the harness cannot
# quietly drift away from the ceiling the GUI actually uses.
app_view() {   # hwmon_dir index -> "ceiling pwm_max enable manual"
    python3 - "$REPO_ROOT" "$HWMON_ROOT" "$1" "$2" 2>/dev/null <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from ksyssupervisor import fanctl
root, target, idx = sys.argv[2], os.path.basename(sys.argv[3]), int(sys.argv[4])
for ch in fanctl.discover_channels(root=root):
    if os.path.basename(ch.hwmon) == target and ch.index == idx:
        st = fanctl.read_state(ch)
        print("%d %d %s %s" % (ch.duty_ceiling, ch.pwm_max, st.enable, st.manual))
        break
else:
    print("NOTFOUND")
PY
}

FD_DIR=""; FD_IDX=""
for key in "${CHANNELS[@]}"; do
    d="${key%:*}"; i="${key##*:}"
    c=$(cat "$d/name" 2>/dev/null)
    case "$FULL_DUTY_CHIPS" in *" $c "*) ;; *) continue ;; esac
    [ -e "${d}/pwm${i}_enable" ] || continue
    [ -e "${d}/fan${i}_input" ] || continue
    # Prefer a header with a fan actually spinning on it, so the RPM reading
    # says something.
    if [ "$(cat "${d}/fan${i}_input" 2>/dev/null || echo 0)" -gt 0 ] 2>/dev/null; then
        FD_DIR="$d"; FD_IDX="$i"; break
    fi
    [ -z "$FD_DIR" ] && { FD_DIR="$d"; FD_IDX="$i"; }
done

if [ -z "$FD_DIR" ]; then
    skip "no channel from the nct6775 family with a tachometer"
else
    fd_chip=$(cat "$FD_DIR/name")
    read -r CEILING PWM_MAX _ _ <<< "$(app_view "$FD_DIR" "$FD_IDX")"

    if [ -z "${CEILING:-}" ] || [ "$CEILING" = "NOTFOUND" ]; then
        fail "the application does not recognise $fd_chip pwm$FD_IDX"
    else
        note "$fd_chip pwm$FD_IDX: the slider's 100% sends $CEILING, not $PWM_MAX"

        if [ "$CEILING" -ge "$PWM_MAX" ]; then
            fail "the slider would still write the top of the range ($CEILING)"
        else
            pass "100% stops short of full duty ($CEILING of $PWM_MAX)"
        fi

        # Section 4 deliberately kills the helper, so this one may need its
        # own session before it can ask for anything.
        if ! kill -0 "$HELPER_PID" 2>/dev/null; then
            note "the previous helper was killed by section 4; starting a fresh session"
            start_helper_session
            reply=$(say "HELLO $PROTO")
            [ "$reply" = "READY $PROTO" ] \
                && pass "second handshake after the crash tests: $reply" \
                || fail "second handshake: got '$reply'"
        fi

        reply=$(say "CLAIM $FD_DIR $FD_IDX")
        case "$reply" in
            OK*) pass "claimed $fd_chip pwm$FD_IDX" ;;
            *)   fail "could not claim $fd_chip pwm$FD_IDX: '$reply'" ;;
        esac

        reply=$(say "SET $FD_DIR $FD_IDX $CEILING")
        case "$reply" in
            OK*) pass "helper accepted the slider's 100% ($CEILING)" ;;
            *)   fail "SET $CEILING refused: '$reply'" ;;
        esac

        sleep "$HOLD"
        got_pwm=$(cat "$FD_DIR/pwm$FD_IDX")
        got_en=$(cat "$FD_DIR/pwm${FD_IDX}_enable")
        got_rpm=$(cat "$FD_DIR/fan${FD_IDX}_input" 2>/dev/null || echo "-")
        note "driver reports pwm=$got_pwm enable=$got_en rpm=$got_rpm"

        # The heart of it: at the slider's 100% the channel must still say
        # "manual", because that is what the GUI reads every second.
        if [ "$got_en" = "1" ]; then
            pass "still reports manual (enable=1) at the slider's 100%"
        else
            fail "reports enable=$got_en at the slider's 100% - the cap did not help"
        fi

        read -r _ _ _ app_manual <<< "$(app_view "$FD_DIR" "$FD_IDX")"
        if [ "$app_manual" = "True" ]; then
            pass "the application agrees the channel is still under manual control"
        else
            fail "the application reads this channel as automatic (manual=$app_manual)"
        fi

        # Now provoke the quirk on purpose, by writing the value the app avoids.
        if [ "$HWMON_ROOT" != "/sys/class/hwmon" ]; then
            skip "not real hardware - a plain file cannot reproduce a driver quirk"
        else
            echo "$PWM_MAX" > "$FD_DIR/pwm$FD_IDX" 2>/dev/null
            # Comfortably past nct6775's own cache window: it refreshes at
            # most once per second (last_updated + HZ), so sleeping exactly one
            # second can read the value from before the write and make the
            # driver look as though it did not alias anything.
            sleep 3
            quirk_pwm=$(cat "$FD_DIR/pwm$FD_IDX")
            quirk_en=$(cat "$FD_DIR/pwm${FD_IDX}_enable")
            note "written $PWM_MAX directly: pwm reads $quirk_pwm, enable reads $quirk_en"

            if [ "$quirk_pwm" != "$PWM_MAX" ]; then
                # Without this the next branch would draw a conclusion about
                # the mode from a duty that never reached full.
                note "the direct write did not land, so this proves nothing either way"
            elif [ "$quirk_en" = "0" ]; then
                pass "reproduced the driver quirk this cap exists to avoid"
            elif [ "$quirk_en" = "1" ]; then
                note "this chip reports manual even at full duty - the cap costs it nothing"
            else
                fail "at full duty the mode reads $quirk_en, which is neither manual nor off"
            fi

            # Whatever it reports, the second guard must hold: the fan is ours.
            read -r _ _ _ app_manual <<< "$(app_view "$FD_DIR" "$FD_IDX")"
            if [ "$app_manual" = "True" ]; then
                pass "the application still reads it as manual at full duty"
            else
                fail "at full duty the application reads automatic - the GUI would grey out a live slider"
            fi

            say "SET $FD_DIR $FD_IDX $CEILING" >/dev/null
        fi

        reply=$(say "AUTO $FD_DIR $FD_IDX")
        case "$reply" in
            OK*) pass "handed $fd_chip pwm$FD_IDX back to the board" ;;
            *)   fail "restore refused: '$reply'" ;;
        esac
    fi
fi

# ---- report -----------------------------------------------------------------

head1 "Result"
echo "  passed: $PASS   failed: $FAIL   skipped: $SKIP"
if [ ${#FAILED_NAMES[@]} -gt 0 ]; then
    echo
    echo -e "${RED}Failures:${NC}"
    printf '  - %s\n' "${FAILED_NAMES[@]}"
fi

echo
echo "Final state (should match the baseline):"
for key in "${CHANNELS[@]}"; do
    dir="${key%:*}"; idx="${key##*:}"
    printf "  %-10s pwm%s pwm=%-4s enable=%-3s (was pwm=%-4s enable=%s)\n" \
        "$(cat "$dir/name")" "$idx" \
        "$(cat "$dir/pwm$idx")" "$(cat "$dir/pwm${idx}_enable" 2>/dev/null)" \
        "${SAVED_PWM[$key]}" "${SAVED_ENABLE[$key]}"
done

[ "$FAIL" -eq 0 ]
