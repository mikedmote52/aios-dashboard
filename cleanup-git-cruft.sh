#!/bin/bash
# cleanup-git-cruft.sh
# One-shot host-side cleanup of accumulated lockfile cruft in
# ~/Desktop/dashboard/.git/. The sandbox mount permits create/rename but
# not unlink(), so months of aborted git operations left behind hundreds
# of *.lock.X aside-renames, probe files, and other detritus. This script
# removes them.
#
# DRY RUN by default — invoke with `--apply` to actually delete.
#
# Safe by construction: only matches known cruft patterns. Never touches:
#   - HEAD, FETCH_HEAD, ORIG_HEAD, COMMIT_EDITMSG, MERGE_HEAD, etc.
#   - index, packed-refs, config, description
#   - hooks/, info/, logs/, refs/heads/main (the real ref), refs/remotes/
#   - objects/ (real loose objects + pack/), except stale tmp_obj_* files
#
# Run once after installing com.moteops.dashboard-publish.plist. Subsequent
# accumulation should be near-zero since git no longer runs from the
# sandbox under publish-push.sh.

set -u

DASH_DIR="$HOME/Desktop/dashboard"
GIT_DIR="$DASH_DIR/.git"

MODE="dryrun"
if [ "${1:-}" = "--apply" ]; then
  MODE="apply"
fi

if [ ! -d "$GIT_DIR" ]; then
  echo "FAIL: $GIT_DIR does not exist."
  exit 1
fi

cd "$DASH_DIR" || exit 1

echo "=== dashboard .git cruft cleanup ($MODE) — $(date) ==="
echo "Target: $GIT_DIR"
echo ""

# Build the candidate list. Each pattern is a known cruft signature.
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# 1. Aside-renamed lockfile leftovers — names that contain ".lock." with
#    suffixes (rmtry, stale, swept, purge, cleared, discard, x_, p17,
#    bak, banished, parked, etc.). Anchored to .git/ and one level deep.
find "$GIT_DIR" -maxdepth 3 -type f \
     \( -name '*.lock.bak*' \
        -o -name '*.lock.rmtry*' \
        -o -name '*.lock.stale*' \
        -o -name '*.lock.swept*' \
        -o -name '*.lock.purge*' \
        -o -name '*.lock.cleared*' \
        -o -name '*.lock.discard*' \
        -o -name '*.lock.x_*' \
        -o -name '*.lock.p17*' \
        -o -name '*.lock.banished*' \
        -o -name '*.lock.parked*' \
        -o -name '*.lock.try*' \
        -o -name '*.lock.rebase*' \
        -o -name '*.lock.cleanup*' \
        -o -name '*.lock.[0-9]*' \) \
     >> "$TMP"

# 2. Probe / test files Mike's runs left behind at .git root.
find "$GIT_DIR" -maxdepth 1 -type f \
     \( -name '_probe*' \
        -o -name '_writetest*' \
        -o -name '_wtest*' \
        -o -name '_permtest' \
        -o -name '_perm_test' \
        -o -name '_testunlink' \
        -o -name '_deltest' \
        -o -name '_lk_*' \
        -o -name '_a_*' \
        -o -name '__test_write' \
        -o -name 'o_excl_test_*' \
        -o -name 'probe_a' \
        -o -name 'probe_c' \
        -o -name 'test_write' \
        -o -name 'testfile' \
        -o -name 'push.err.discard' \) \
     >> "$TMP"

# 3. Stale tmp_obj_* in objects/ — git itself deletes these after 30 min
#    when it can. Anything older than an hour is definitely orphaned.
find "$GIT_DIR/objects" -maxdepth 2 -type f -name 'tmp_obj_*' -mmin +60 \
     >> "$TMP"

# 4. Aside-renamed real refs in refs/heads/. Real ref is .git/refs/heads/main.
#    Anything else there (main.lock.purge.X etc.) is cruft.
find "$GIT_DIR/refs/heads" -maxdepth 1 -type f ! -name 'main' \
     >> "$TMP"

# 5. _parked_locks/ — Mike's custom quarantine folder, not git-native.
#    If present, remove entire subtree.
if [ -d "$GIT_DIR/_parked_locks" ]; then
  find "$GIT_DIR/_parked_locks" -type f >> "$TMP"
fi

# 6. quarantine/ — same story as _parked_locks. Custom workaround dir.
if [ -d "$GIT_DIR/quarantine" ]; then
  find "$GIT_DIR/quarantine" -type f >> "$TMP"
fi

# Dedup
sort -u "$TMP" -o "$TMP"

TOTAL=$(wc -l < "$TMP" | tr -d ' ')
echo "Cruft files identified: $TOTAL"
echo ""

if [ "$TOTAL" -eq 0 ]; then
  echo "Nothing to clean. Exiting."
  exit 0
fi

# Show a sample
echo "Sample (first 20):"
head -20 "$TMP" | sed 's/^/  /'
[ "$TOTAL" -gt 20 ] && echo "  ... and $((TOTAL - 20)) more"
echo ""

if [ "$MODE" = "dryrun" ]; then
  echo "DRY RUN — no files deleted. Re-run with --apply to delete."
  exit 0
fi

# Apply: delete and report
DELETED=0
FAILED=0
while IFS= read -r f; do
  if [ -z "$f" ]; then continue; fi
  if rm -f -- "$f" 2>/dev/null; then
    DELETED=$((DELETED + 1))
  else
    FAILED=$((FAILED + 1))
    echo "FAILED: $f"
  fi
done < "$TMP"

# Try to remove now-empty directories
for d in "$GIT_DIR/_parked_locks" "$GIT_DIR/quarantine"; do
  if [ -d "$d" ] && [ -z "$(ls -A "$d" 2>/dev/null)" ]; then
    rmdir "$d" 2>/dev/null && echo "Removed empty dir: $d"
  elif [ -d "$d" ]; then
    # Has subdirs (rebase-merge etc.) — list and try to clean recursively
    find "$d" -depth -type d -empty -delete 2>/dev/null
    if [ -z "$(ls -A "$d" 2>/dev/null)" ]; then
      rmdir "$d" 2>/dev/null && echo "Removed empty dir: $d"
    else
      echo "Note: $d still has contents — review manually."
    fi
  fi
done

echo ""
echo "Deleted: $DELETED"
echo "Failed:  $FAILED"
echo ""
echo "Verifying real git state still intact:"
if git -C "$DASH_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
  echo "  HEAD ok: $(git -C "$DASH_DIR" rev-parse HEAD)"
else
  echo "  WARN: HEAD no longer resolves. Investigate."
  exit 1
fi
if git -C "$DASH_DIR" rev-parse --verify refs/heads/main >/dev/null 2>&1; then
  echo "  refs/heads/main ok: $(git -C "$DASH_DIR" rev-parse refs/heads/main)"
else
  echo "  WARN: refs/heads/main no longer resolves. Investigate."
  exit 1
fi
echo "Done."
