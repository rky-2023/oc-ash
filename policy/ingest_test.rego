# Tests for the ingest filter policy (Phase 3 task 3.5).
# Run: opa test policy/

package openclaw.ingest_test

import data.openclaw.ingest
import future.keywords.if

# ── fswatch: noise paths drop ───────────────────────────────────────────────

test_fswatch_dist_drops if {
	ingest.decision == "drop" with input as {"source": "fswatch", "path": "/home/asher/openclaw/dist/bundle.js"}
}

test_fswatch_build_drops if {
	ingest.decision == "drop" with input as {"source": "fswatch", "path": "/home/asher/ashboard-backend/build/out.bin"}
}

test_fswatch_coverage_drops if {
	ingest.decision == "drop" with input as {"source": "fswatch", "path": "/home/asher/openclaw/coverage/lcov.info"}
}

test_fswatch_logfile_drops if {
	ingest.decision == "drop" with input as {"source": "fswatch", "path": "/home/asher/openclaw/server.log"}
}

# ── fswatch: real edits keep ────────────────────────────────────────────────

test_fswatch_plan_keeps if {
	ingest.decision == "keep" with input as {"source": "fswatch", "path": "/home/asher/openclaw/PLAN.md"}
}

test_fswatch_src_keeps if {
	ingest.decision == "keep" with input as {"source": "fswatch", "path": "/home/asher/openclaw/core/app/main.py"}
}

# "distinct" is not a "dist" segment — must not false-positive.
test_fswatch_distinct_dir_keeps if {
	ingest.decision == "keep" with input as {"source": "fswatch", "path": "/home/asher/openclaw/distinct/notes.md"}
}

# ── git: always keep ────────────────────────────────────────────────────────

test_git_keeps if {
	ingest.decision == "keep" with input as {"source": "git", "hook": "post-commit", "repo": "openclaw"}
}

# ── claude: empty Notification drops, real ones keep ────────────────────────

test_claude_empty_notification_drops if {
	ingest.decision == "drop" with input as {"source": "claude", "hook": "Notification", "body": ""}
}

test_claude_missing_body_notification_drops if {
	ingest.decision == "drop" with input as {"source": "claude", "hook": "Notification"}
}

test_claude_notification_with_body_keeps if {
	ingest.decision == "keep" with input as {"source": "claude", "hook": "Notification", "body": "build finished"}
}

test_claude_tool_hook_keeps if {
	ingest.decision == "keep" with input as {"source": "claude", "hook": "PreToolUse", "body": ""}
}

# ── gh-poller: no-op drops, real change keeps ───────────────────────────────

test_gh_noop_drops if {
	ingest.decision == "drop" with input as {"source": "gh-poller", "changed": false}
}

test_gh_change_keeps if {
	ingest.decision == "keep" with input as {"source": "gh-poller", "changed": true}
}

# Absent `changed` must not drop (err on inclusion).
test_gh_missing_changed_keeps if {
	ingest.decision == "keep" with input as {"source": "gh-poller", "entity": "pull_request"}
}

# ── unknown source defaults to keep ─────────────────────────────────────────

test_unknown_source_keeps if {
	ingest.decision == "keep" with input as {"source": "mystery", "path": "/whatever/dist/x.log"}
}
