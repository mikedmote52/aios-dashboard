#!/bin/bash
# publish-push.sh
# Host-side commit + push for ~/Desktop/dashboard. Driven by launchd
# (com.moteops.dashboard-publish.plist). The Cowork-side dashboard-publish
# skill builds the snapshot files in the sandbox; this script — running in
# the host shell where unlink() works normally — handles the commit/push.
#
# Mirrors the publish_notifications.sh pattern Mike already uses for
# notification-router: log to /tmp, clean stale locks, idempotent commit,
# best-effort push. Safe to run more frequently than the build cadence —
# if nothing changed, no commit happens.
#
# History: dashboard-publish ran git from the sandbox for months. The
# Cowork mount permits create/rename but not unlink(), so every aborted
# git operation left behind index.lock / HEAD.lock / refs/heads/main.lock
# files that the next run could not delete. Over time .git accumulated
# hundreds of *.lock.* leftovers and the renamed-aside cruft (*.x_*,
# *.cleared_*, etc.) you see in cleanup-git-cruft.sh. Moving git into the
# host shell fixes the root cause.

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$PATH"

LOG=/tmp/dashboard_publish_push.log
# Real repo lives OFF the Desktop so a launchd-spawned process can read it
# without a TCC grant. ~/Desktop/dashboard is a symlink to this path; the
# Cowork build writes through that symlink, but git here MUST use the real
# off-Desktop path — a CWD inside the TCC-protected Desktop tree returns
# "Operation not permitted" when git reads the working directory.
DASH_DIR="$HOME/dashboard"

echo "=== START $(date) ===" >> "$LOG"

if [ ! -d "$DASH_DIR/.git" ]; then
  echo "FAIL: $DASH_DIR is not a git checkout." >> "$LOG"
  exit 1
fi

cd "$DASH_DIR" || { echo "FAIL: cannot cd to $DASH_DIR" >> "$LOG"; exit 1; }

# Step 1: clean stale lockfiles. The host can actually unlink, so these go
# away cleanly. Targets: the canonical Git lockfile names AND any leftover
# .lock.X aside-renames that prior sandbox runs created.
rm -f .git/HEAD.lock \
      .git/index.lock \
      .git/ORIG_HEAD.lock \
      .git/FETCH_HEAD.lock \
      .git/refs/heads/main.lock \
      .git/objects/maintenance.lock 2>>"$LOG"

# Sweep aside-renamed leftover lock variants (stale, swept, purge, cleared,
# rmtry, discard, x_, p17*, parked, etc.) — these accumulated under the
# sandbox-can't-unlink regime.
find .git -maxdepth 3 \
     \( -name '*.lock.*' \
        -o -name 'HEAD.lock.*' \
        -o -name 'index.lock.*' \
        -o -name '*.lock.bak*' \
        -o -name 'HEAD.lock.bak*' \) \
     -type f -delete 2>>"$LOG"

# Quarantine dir is Mike's custom workaround folder — not git's native.
# If empty, remove it; if not empty, leave alone for manual review.
if [ -d .git/quarantine ] && [ -z "$(ls -A .git/quarantine 2>/dev/null)" ]; then
  rmdir .git/quarantine 2>>"$LOG"
fi

# Stale tmp_obj_* in objects/ older than 30 minutes (git's own threshold)
find .git/objects -maxdepth 2 -name 'tmp_obj_*' -type f -mmin +30 -delete 2>>"$LOG"

# Step 2: commit if there are changes. `git add -A` then check the index
# diff; no-op commit is fine, we just won't push it.
git add -A 2>>"$LOG"

if git diff --cached --quiet 2>/dev/null; then
  # Possible the working tree already had committed-but-unpushed changes
  # from a prior sandbox run. We'll still try to push below.
  echo "No staged changes; checking for unpushed commits." >> "$LOG"
else
  git -c user.name="dashboard-publish" -c user.email="dashboard-publish@moteops.local" \
      commit -m "auto-publish $(date -u +%Y-%m-%dT%H%M)" >> "$LOG" 2>&1
  COMMIT_RC=$?
  if [ $COMMIT_RC -ne 0 ]; then
    echo "WARN: commit exited $COMMIT_RC" >> "$LOG"
  fi
fi

# Step 3: push if local is ahead of origin. Best-effort; failures get logged.
LOCAL=$(git rev-parse HEAD 2>/dev/null)
REMOTE=$(git rev-parse @{u} 2>/dev/null)
if [ -n "$LOCAL" ] && [ "$LOCAL" != "$REMOTE" ]; then
  git push origin main >> "$LOG" 2>&1
  PUSH_RC=$?
  if [ $PUSH_RC -ne 0 ]; then
    echo "WARN: git push exited $PUSH_RC" >> "$LOG"
  else
    echo "Pushed $LOCAL to origin/main." >> "$LOG"
  fi
else
  echo "Nothing to push (local == origin/main at $LOCAL)." >> "$LOG"
fi

# Step 4: refresh FETCH_HEAD so the next run's compare is accurate.
git fetch origin --quiet 2>>"$LOG"

echo "=== DONE $(date) ===" >> "$LOG"
exit 0
