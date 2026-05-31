# Ingest filter policy for the event ingesters (Phase 3 task 3.5).
#
# Each ingester (fswatch, git hooks, claude hooks, gh-poller) queries
# `data.openclaw.ingest.decision` BEFORE publishing to NATS, with:
#   input = {
#     "source": "fswatch" | "git" | "claude" | "gh-poller",
#     # fswatch:
#     "path":   "/home/asher/openclaw/dist/build.log",
#     # claude:
#     "hook":   "Notification" | "PreToolUse" | ...,
#     "body":   "<hook payload text>",
#     # gh-poller:
#     "changed": true | false,   # did state actually change since checkpoint?
#   }
#
# It returns a single decision:
#   "keep" → publish the event to the bus (default).
#   "drop" → swallow it; never reaches the audit ledger.
#
# Design rule (ADR-003): the audit log is a RECORD — err on inclusion.
# We only drop the most boring, high-volume noise; everything else is kept.

package openclaw.ingest

import future.keywords.if
import future.keywords.in

# Default: keep. The ledger is meant to be a record; only explicit noise drops.
default decision := "keep"

# fswatch — drop build/output/log noise (the fswatch exclusion list already
# strips the worst churn; this catches generated artifacts that slip through).
decision := "drop" if {
	input.source == "fswatch"
	_is_noise_path(input.path)
}

# claude — drop Notification hooks with an empty body (nothing to record).
decision := "drop" if {
	input.source == "claude"
	input.hook == "Notification"
	_blank(object.get(input, "body", ""))
}

# gh-poller — drop no-op polls (only timestamps moved; no real state change).
decision := "drop" if {
	input.source == "gh-poller"
	input.changed == false
}

# git hooks fire rarely and are always meaningful → no drop rule (default keep).

# Convenience boolean for callers that prefer a truthy check.
keep if decision == "keep"

# ── helpers ────────────────────────────────────────────────────────────────

# Generated-artifact globs. `**` crosses path separators; `*` stays within a
# segment (delimiter "/").
_noise_globs := [
	"**/dist/**",
	"**/build/**",
	"**/coverage/**",
	"**/*.log",
]

_is_noise_path(path) if {
	some g in _noise_globs
	glob.match(g, ["/"], path)
}

_blank(s) if s == ""

_blank(s) if s == null
