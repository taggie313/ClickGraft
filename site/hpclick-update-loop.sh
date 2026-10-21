#!/bin/bash
#
# HP Click leaves a failed updater running in a loop. This finds it, and can
# stop it.
#
#   ./hpclick-update-loop.sh          # look only, change nothing
#   ./hpclick-update-loop.sh --fix    # stop it
#   ./hpclick-update-loop.sh --undo   # put everything back
#
# What is wrong: in HP Click 4.11.31 and earlier, the updater's helper (ShipIt)
# looks for a component called Mantle in two directories, and HP ships it in a
# third. The helper cannot start at all. macOS has been told to keep restarting
# it until it succeeds, which it never will, so it restarts about every two
# seconds, forever, writing a line to a log file each time. That log reaches
# gigabytes. HP fixed this in 4.11.32 -- but the only way to receive 4.11.32 is
# through the updater that is broken, so it cannot arrive on its own.
#
# What this does: tells macOS to stop restarting the helper, and deletes the
# log. It does NOT modify HP Click, or any other application. It needs no
# password. --undo reverses it completely.
#
set -u

LABEL="com.hp.hpclick.ShipIt"
CACHE="$HOME/Library/Caches/$LABEL"
LOG="$CACHE/ShipIt_stderr.log"
DOMAIN="gui/$(id -u)"

bold=$(tput bold 2>/dev/null || true); off=$(tput sgr0 2>/dev/null || true)
say() { printf '%s\n' "$*"; }
hdr() { printf '\n%s%s%s\n' "$bold" "$*" "$off"; }

human() {  # bytes -> human, without needing numfmt
  awk -v b="${1:-0}" 'BEGIN{ s="B KB MB GB TB"; split(s,u," "); i=1;
    while (b>=1024 && i<5) { b/=1024; i++ }
    printf (i==1 ? "%d %s" : "%.1f %s"), b, u[i] }'
}

# Every HP Click on this Mac, by bundle id rather than by name.
find_clicks() {
  for app in /Applications/*.app; do
    [ -d "$app" ] || continue
    id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' \
         "$app/Contents/Info.plist" 2>/dev/null) || continue
    [ "$id" = "com.hp.hpclick" ] && printf '%s\n' "$app"
  done
}

version_of() {
  /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
    "$1/Contents/Info.plist" 2>/dev/null || echo "?"
}

# Ask the helper itself whether it can start. Run with no arguments it does
# nothing but print a usage line and quit, so this is safe -- and unlike
# reading the version number it tests the actual fault. Needs no developer
# tools, which matters: most people affected by this do not have Xcode.
shipit_state() {  # -> broken | ok | absent
  local s="$1/Contents/Frameworks/Squirrel.framework/Versions/A/ShipIt"
  [ -x "$s" ] || s="$1/Contents/Frameworks/Squirrel.framework/Resources/ShipIt"
  [ -x "$s" ] || { echo absent; return; }
  # Judge by what it SAYS, not by its exit code: a working ShipIt given no
  # arguments still exits non-zero (it has nothing to do), so the code alone
  # cannot tell "it ran and complained" from "it never started".
  local out; out=$("$s" 2>&1)
  if printf '%s' "$out" | grep -q 'Library not loaded'; then
    echo broken                     # dyld killed it before main()
  elif printf '%s' "$out" | grep -Eq 'Missing launchd job label|ShipIt quitting'; then
    echo ok                         # it got far enough to log its own messages
  else
    echo unknown
  fi
}

job_runs() { launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/successive crashes =/{print $4; exit}'; }
job_present() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
# BSD grep has no \| in basic expressions, so -E here, not -q with BRE.
is_disabled() { launchctl print-disabled "$DOMAIN" 2>/dev/null | grep -Eq "\"$LABEL\" => (disabled|true)"; }

report() {
  local any_broken=0 any_ok=0
  hdr "HP Click installations"
  local found=0
  while IFS= read -r app; do
    [ -n "$app" ] || continue
    found=1
    local v st note
    v=$(version_of "$app"); st=$(shipit_state "$app")
    case "$st" in
      broken) note="updater is broken -- this is the one that loops"; any_broken=1 ;;
      ok)     note="updater works (4.11.32 or newer)"; any_ok=1 ;;
      absent) note="no updater present" ;;
      *)      note="could not tell" ;;
    esac
    printf '  %-34s %-9s %s\n' "$(basename "$app")" "$v" "$note"
  done <<EOF
$(find_clicks)
EOF
  [ "$found" = 1 ] || say "  none found in /Applications"

  hdr "The runaway updater"
  if job_present; then
    local r; r=$(job_runs)
    say "  status      : RUNNING -- macOS is restarting it about every 2 seconds"
    [ -n "${r:-}" ] && say "  failed runs : $r"
  elif is_disabled; then
    say "  status      : stopped, and blocked from starting again"
  else
    say "  status      : not running"
  fi

  if [ -f "$LOG" ]; then
    local sz; sz=$(wc -c < "$LOG" 2>/dev/null | tr -d ' ')
    say "  log file    : $(human "$sz")   ($LOG)"
  else
    say "  log file    : none"
  fi

  BROKEN_PRESENT=$any_broken
  WORKING_PRESENT=$any_ok
}

do_fix() {
  hdr "Stopping it"
  if job_present; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null && say "  stopped the running updater" \
      || say "  could not stop it (it may have already exited)"
  else
    say "  nothing running"
  fi
  if launchctl disable "$DOMAIN/$LABEL" 2>/dev/null; then
    say "  told macOS not to start it again"
  else
    say "  could not set the block -- the loop may return when HP Click next opens"
  fi
  local freed=0
  [ -f "$LOG" ] && freed=$(wc -c < "$LOG" 2>/dev/null | tr -d ' ')
  rm -f "$CACHE/ShipIt_stderr.log" "$CACHE/ShipIt_stdout.log" "$CACHE/ShipItState.plist" 2>/dev/null
  [ "${freed:-0}" -gt 0 ] 2>/dev/null && say "  deleted the log, freeing $(human "$freed")"
}

do_undo() {
  hdr "Undoing"
  launchctl enable "$DOMAIN/$LABEL" 2>/dev/null \
    && say "  HP Click may submit its updater again (back to how it was)" \
    || say "  could not clear the block"
  say "  nothing else was ever changed, so there is nothing else to restore"
}

verdict() {
  hdr "Result"
  local bad=0
  job_present && { say "  ✗ the updater is still running"; bad=1; }
  [ -f "$LOG" ] && { say "  ✗ the log is still there"; bad=1; }
  is_disabled || { say "  ✗ it is not blocked, so it can come back"; bad=1; }
  if [ "$bad" = 0 ]; then
    say "  ✓ nothing is running"
    say "  ✓ the log is gone"
    say "  ✓ it cannot start again"
    say ""
    say "  Clean. HP Click itself was not touched and will open as usual."
    if [ "${WORKING_PRESENT:-0}" = 1 ]; then
      say ""
      say "  Note: you also have a version whose updater works. While the block"
      say "  is in place that one cannot auto-update either. Run --undo to lift"
      say "  it, or just download new versions from HP's site by hand."
    fi
    if [ "${BROKEN_PRESENT:-0}" = 1 ]; then
      say ""
      say "  To stop needing this at all, install HP Click 4.11.32 or newer from"
      say "  HP's download page -- the bug is fixed there. Check it lists your"
      say "  printer first; some older DesignJets were dropped after 4.8.117."
    fi
  fi
}

case "${1:-}" in
  --fix)
    report
    if [ "${BROKEN_PRESENT:-0}" = 0 ] && ! job_present && [ ! -f "$LOG" ]; then
      hdr "Result"; say "  ✓ Nothing to fix on this Mac."; exit 0
    fi
    do_fix; report >/dev/null; verdict ;;
  --undo)
    do_undo; report; verdict >/dev/null
    hdr "Result"; say "  Back to how it was." ;;
  ""|--check)
    report
    hdr "What to do"
    if job_present || [ -f "$LOG" ]; then
      say "  This Mac is affected. Run the same command with --fix to stop it:"
      say "      $0 --fix"
      say "  Nothing is changed until you do. No password needed."
    elif [ "${BROKEN_PRESENT:-0}" = 1 ]; then
      say "  You have a version with the broken updater, but it is not looping"
      say "  right now. It will start the next time that HP Click is opened and"
      say "  finds an update. Running --fix now prevents that."
    else
      say "  Nothing wrong here."
    fi ;;
  *) say "usage: $0 [--check | --fix | --undo]"; exit 2 ;;
esac
