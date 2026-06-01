#!/usr/bin/env python3
"""Install the oc-ingest Claude Code hooks into a settings.json (task 3.3).

Merges a `hooks` block that forwards each lifecycle event to
`oc-claude-hook.sh` (which POSTs to the oc-ingest unix socket). Existing,
non-oc hooks are preserved.

SAFETY: dry-run by default — prints the merged result without writing. Pass
--apply to write (a timestamped .bak is made first). The default target is
~/.claude/settings.json; override with --settings. Because that file also
configures the running Claude Code harness, review the diff before --apply.

Usage:
  python install-claude-hooks.py                  # dry-run, show merge
  python install-claude-hooks.py --apply          # write ~/.claude/settings.json
  python install-claude-hooks.py --settings X --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

HOOK_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "SessionStart",
    "Notification",
]


def _command(script: str) -> str:
    return script


def build_hooks_block(script_path: str) -> dict:
    entry = {"matcher": "", "hooks": [{"type": "command", "command": _command(script_path)}]}
    return {ev: [entry] for ev in HOOK_EVENTS}


def _is_oc_entry(entry: dict, script_path: str) -> bool:
    for h in entry.get("hooks", []):
        if script_path in h.get("command", ""):
            return True
    return False


def merge(existing: dict, script_path: str) -> dict:
    out = dict(existing)
    hooks = dict(out.get("hooks", {}))
    block = build_hooks_block(script_path)
    for ev, entries in block.items():
        cur = [e for e in hooks.get(ev, []) if not _is_oc_entry(e, script_path)]
        hooks[ev] = cur + entries
    out["hooks"] = hooks
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    ap.add_argument(
        "--script",
        default=str(Path(__file__).resolve().parent / "oc-claude-hook.sh"),
        help="Absolute path to oc-claude-hook.sh that Claude Code will run.",
    )
    ap.add_argument("--apply", action="store_true", help="Write the file (default: dry-run).")
    args = ap.parse_args()

    target = Path(args.settings)
    existing = {}
    if target.exists():
        existing = json.loads(target.read_text() or "{}")

    merged = merge(existing, args.script)
    rendered = json.dumps(merged, indent=2)

    if not args.apply:
        print("# DRY RUN — would write the following to", target)
        print("# (run with --apply to write; a .bak is made first)\n")
        print(rendered)
        print("\n# Ensure the script is executable: chmod +x", args.script)
        return 0

    if target.exists():
        bak = target.with_suffix(f".json.bak-{dt.datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(target, bak)
        print(f"backed up {target} → {bak}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered + "\n")
    print(f"wrote {target}")
    print(f"reminder: chmod +x {args.script}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
