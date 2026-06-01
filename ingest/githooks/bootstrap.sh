#!/usr/bin/env bash
# Point every repo under /home/asher/* at the shared oc-ingest git hooks
# (Phase 3 task 3.2). Idempotent — safe to re-run after adding repos.
#
# CAVEAT: `core.hooksPath` REPLACES a repo's own .git/hooks. If a repo relies
# on local hooks (husky, pre-commit, signing), those stop running. Review per
# repo before running. Also: while the oc-ingest worker is DOWN, each hook
# still returns in <=3s (curl timeout) and exits 0, so git stays usable but
# commits incur that timeout — keep the worker up or unset hooksPath.
set -euo pipefail

HOOKS_DIR="${OC_GITHOOKS_DIR:-/home/asher/openclaw/ingest/githooks/hooks}"
ROOT_GLOB="${OC_GITHOOKS_ROOT:-/home/asher}"

[ -d "$HOOKS_DIR" ] || { echo "hooks dir not found: $HOOKS_DIR" >&2; exit 1; }
chmod +x "$HOOKS_DIR"/* 2>/dev/null || true

count=0
for dir in "$ROOT_GLOB"/*/.git; do
  [ -e "$dir" ] || continue
  repo_root=$(dirname "$dir")
  git -C "$repo_root" config core.hooksPath "$HOOKS_DIR"
  echo "  core.hooksPath set → $repo_root"
  count=$((count + 1))
done
echo "configured $count repo(s) to use $HOOKS_DIR"
echo "verify: git -C <repo> config core.hooksPath"
