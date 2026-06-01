# Tests for redaction.rego. Run: `opa test policy/` (CI gate per policy/README).

package openclaw.redaction

import future.keywords

# ── Drop: secret-looking keys ────────────────────────────────────────
test_drop_api_key if {
	decisions["api_key"] == "drop" with input as _mk("oc.event.core.request.post", {"api_key": "sk_live_x"})
}

test_drop_authorization if {
	decisions["authorization"] == "drop" with input as _mk("oc.event.core.request.post", {"authorization": "Bearer x"})
}

test_drop_access_token if {
	decisions["access_token"] == "drop" with input as _mk("oc.event.core.request.post", {"access_token": "x"})
}

# "author" must NOT match the bare-auth pattern (token boundaries).
test_keep_author_not_drop if {
	decisions["author"] == "keep" with input as _mk("oc.event.git.commit", {"author": "rky"})
}

# ── Encrypt: subject-scoped sensitive content ────────────────────────
test_encrypt_mail_body if {
	decisions["body"] == "encrypt" with input as _mk("oc.event.mail.received", {"body": "hello"})
}

test_encrypt_calendar_description if {
	decisions["description"] == "encrypt" with input as _mk("oc.event.calendar.created", {"description": "dentist"})
}

test_encrypt_fs_contents if {
	decisions["contents"] == "encrypt" with input as _mk("oc.mcp.fs-asher.read", {"contents": "file data"})
}

# body under a NON-mail subject is ordinary content → keep.
test_keep_body_when_not_mail if {
	decisions["body"] == "keep" with input as _mk("oc.event.core.request.post", {"body": "x"})
}

# ── Keep: ordinary metadata ──────────────────────────────────────────
test_keep_method if {
	decisions["method"] == "keep" with input as _mk("oc.event.core.request.post", {"method": "GET"})
}

# A secret key wins over an encrypt-subject (drop is the safe outcome).
test_drop_beats_encrypt if {
	decisions["api_key"] == "drop" with input as _mk("oc.event.mail.received", {"api_key": "x"})
}

# Falsy values are still classified (object.keys, not truthiness iteration).
test_classifies_falsy_value if {
	decisions["password"] == "drop" with input as _mk("oc.event.core.request.post", {"password": ""})
}

# session_id is a correlation id, not a secret → keep (bare `session` removed
# from the regex). Real session secrets are still dropped.
test_keep_session_id if {
	decisions["session_id"] == "keep" with input as _mk("oc.event.claude.PreToolUse", {"session_id": "s-123"})
}

test_drop_session_token if {
	decisions["session_token"] == "drop" with input as _mk("oc.event.core.request.post", {"session_token": "x"})
}

test_drop_session_secret if {
	decisions["session_secret"] == "drop" with input as _mk("oc.event.core.request.post", {"session_secret": "x"})
}

_mk(subject, payload) := {"subject": subject, "action": "event", "payload": payload}
