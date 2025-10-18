#!/usr/bin/env bash
set -euo pipefail

# Sync upstream changes from l429609201/misaka_danmu_server into this fork
# while preserving Plus-specific files, so you can have "the best of both".
#
# Usage:
#   scripts/upstream_sync.sh [upstream_branch]
#
# Notes:
# - This script creates a new merge branch: merge/upstream-YYYYmmdd-HHMMSS
# - It temporarily configures .gitattributes to force-keep our versions for
#   Plus-specific files and prefers upstream (theirs) for other conflicts to
#   pull in the latest improvements from upstream.
# - After the merge, it cleans up temporary attributes.
#
# Requirements:
# - git must be installed and repository clean (no uncommitted changes).
# - network access to GitHub.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$ROOT_DIR"

UPSTREAM_REMOTE="upstream"
UPSTREAM_URL="https://github.com/l429609201/misaka_danmu_server.git"
UPSTREAM_BRANCH="${1:-main}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[ERROR] Not inside a git repository: $ROOT_DIR" >&2
  exit 1
fi

# Ensure working tree clean
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[ERROR] Working tree is not clean. Commit or stash your changes first." >&2
  exit 1
fi

# Add upstream remote if missing
if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
  echo "[INFO] Adding upstream remote: $UPSTREAM_URL"
  git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
fi

echo "[INFO] Fetching upstream..."
git fetch --tags "$UPSTREAM_REMOTE"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
MERGE_BRANCH="merge/upstream-$(date +%Y%m%d-%H%M%S)"

echo "[INFO] Creating merge branch: $MERGE_BRANCH (from $CURRENT_BRANCH)"
git checkout -b "$MERGE_BRANCH" "$CURRENT_BRANCH"

# Configure an 'ours' merge driver for selective files
# This keeps our local version for Plus-specific files while pulling upstream for the rest.
# Builtin 'ours' driver can be defined as returning exit 0 without modifying files.
if ! git config --get merge.ours.driver >/dev/null 2>&1; then
  git config merge.ours.driver true
fi

ATTR_FILE=".gitattributes"
BACKUP_ATTR_FILE=".gitattributes.backup.$(date +%s)"
if [ -f "$ATTR_FILE" ]; then
  cp "$ATTR_FILE" "$BACKUP_ATTR_FILE"
fi

echo "[INFO] Writing temporary .gitattributes rules to preserve Plus features..."
{
  echo "# --- BEGIN: upstream-sync temporary rules ---"
  echo "src/api/control_api.py merge=ours"
  echo "src/api/invite_api.py merge=ours"
  echo "src/rate_limiter.py merge=ours"
  echo "src/rate_limit/* merge=ours"
  echo "src/config_manager.py merge=ours"
  echo "src/main.py merge=ours"
  echo "src/orm_models.py merge=ours"
  echo "src/crud.py merge=ours"
  echo "src/models.py merge=ours"
  echo "web/src/pages/register/** merge=ours"
  echo "web/src/pages/setting/components/Invites.jsx merge=ours"
  echo "web/src/pages/setting/components/Accounts.jsx merge=ours"
  echo "web/src/apis/index.js merge=ours"
  echo "# --- END: upstream-sync temporary rules ---"
} >> "$ATTR_FILE"

git add "$ATTR_FILE"
TEMP_COMMIT_MSG="chore(upstream-sync): add temporary merge attributes"
git commit -m "$TEMP_COMMIT_MSG" || true

set +e
# Merge upstream with preference for upstream changes on conflicts, except our protected files above
GIT_MERGE_AUTOEDIT=no git merge -s recursive -X theirs "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
MERGE_STATUS=$?
set -e

if [ $MERGE_STATUS -ne 0 ]; then
  echo "[WARN] Merge reported conflicts. Please resolve them manually, then run:"
  echo "       git add -A && git commit"
  echo "[INFO] You can keep our versions for Plus-specific files; this script already set .gitattributes to prefer ours there."
else
  echo "[INFO] Merge completed without conflicts."
fi

# Remove temporary .gitattributes rules
if [ -f "$ATTR_FILE" ]; then
  echo "[INFO] Cleaning up temporary .gitattributes rules..."
  # Filter out the block between BEGIN and END
  awk '/# --- BEGIN: upstream-sync temporary rules ---/{flag=1;next}/# --- END: upstream-sync temporary rules ---/{flag=0;next}!flag{print}' "$ATTR_FILE" > "$ATTR_FILE.tmp"
  mv "$ATTR_FILE.tmp" "$ATTR_FILE"
  git add "$ATTR_FILE"
  git commit -m "chore(upstream-sync): remove temporary merge attributes" || true
fi

echo
echo "[DONE] Upstream merge branch created: $MERGE_BRANCH"
echo "       Review changes, run tests, and then merge it back into $CURRENT_BRANCH when ready."
