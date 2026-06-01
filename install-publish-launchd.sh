#!/bin/bash
# install-publish-launchd.sh
# One-shot installer + verifier for the dashboard-publish host-side launchd
# job. Run this on the host (Terminal, not the sandbox).
#
# Does:
#   1. Copies com.moteops.dashboard-publish.plist into ~/Library/LaunchAgents/
#   2. launchctl load -w'd it (registers + enables)
#   3. Runs publish-push.sh once immediately as a smoke test
#   4. Reports origin/main SHA before/after + tail of the log
#
# Idempotent — safe to re-run.

set -u

DASH_DIR="$HOME/Desktop/dashboard"
PLIST_SRC="$DASH_DIR/com.moteops.dashboard-publish.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.moteops.dashboard-publish.plist"
SCRIPT="$DASH_DIR/publish-push.sh"
LABEL="com.moteops.dashboard-publish"
LOG=/tmp/dashboard_publish_push.log

echo "=== dashboard-publish launchd installer — $(date) ==="

if [ ! -f "$PLIST_SRC" ]; then
  echo "FAIL: $PLIST_SRC not found."
  exit 1
fi
if [ ! -x "$SCRIPT" ]; then
  echo "FAIL: $SCRIPT not executable. chmod +x first."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Unload existing if present
if launchctl list | grep -q "$LABEL"; then
  echo "Unloading existing $LABEL..."
  launchctl unload -w "$PLIST_DST" 2>/dev/null || true
fi

cp "$PLIST_SRC" "$PLIST_DST"
echo "Copied plist to: $PLIST_DST"

launchctl load -w "$PLIST_DST"
if [ $? -ne 0 ]; then
  echo "FAIL: launchctl load failed."
  exit 1
fi
echo "Loaded launchd job: $LABEL"
echo ""

echo "=== Immediate smoke-test: running publish-push.sh ==="
SHA_BEFORE=$(git -C "$DASH_DIR" ls-remote origin main 2>/dev/null | awk '{print $1}')
echo "origin/main before: ${SHA_BEFORE:-<unknown>}"

bash "$SCRIPT"
SHA_LOCAL=$(git -C "$DASH_DIR" rev-parse HEAD 2>/dev/null)
SHA_AFTER=$(git -C "$DASH_DIR" ls-remote origin main 2>/dev/null | awk '{print $1}')

echo ""
echo "local HEAD:         ${SHA_LOCAL:-<unknown>}"
echo "origin/main after:  ${SHA_AFTER:-<unknown>}"

if [ "$SHA_AFTER" = "$SHA_LOCAL" ] && [ -n "$SHA_AFTER" ]; then
  echo "OK — origin/main is in sync with local HEAD."
elif [ -n "$SHA_AFTER" ] && [ "$SHA_AFTER" != "$SHA_BEFORE" ]; then
  echo "OK — origin/main advanced (push landed)."
else
  echo "NOTE — origin/main did not advance. Check $LOG for details."
fi
echo ""

echo "=== Last 30 lines of $LOG ==="
tail -30 "$LOG" 2>/dev/null || echo "(no log yet)"
echo ""

echo "Next scheduled fires (from launchd):"
launchctl list "$LABEL" 2>/dev/null | head -20 || echo "(label not registered?)"
echo ""

echo "Install complete. The job will fire every :08 from 06:00 through 22:00 PT."
echo "To uninstall:"
echo "  launchctl unload -w $PLIST_DST"
echo "  rm $PLIST_DST"
