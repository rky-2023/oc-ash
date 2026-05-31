# Redaction policy for the audit pipeline (ADR-003 D6, Phase 2 task 2.7).
#
# The audit-appender queries `data.openclaw.redaction.decisions` with:
#   input = {
#     "subject": "<envelope subject, e.g. oc.event.mail.received>",
#     "action":  "<request|response|event|decision>",
#     "payload": { <field>: <value>, ... }   # the raw redacted_payload
#   }
#
# It returns one decision per top-level payload field:
#   "drop"    → value replaced with "<redacted:secret>"; original is GONE.
#   "encrypt" → value sealed (AES-256-GCM, DEK wrapped by Vault transit
#               audit-pii-v3); shown as "<encrypted:...>" + an encrypted_blobs entry.
#   "keep"    → value stored verbatim.
#
# Philosophy: for SECRETS, default-drop (fail safe — a missed secret is
# unrecoverable harm in a WORM ledger). For ordinary content, default-keep
# (it's audit data we want to retain). The appender ALSO applies a
# Shannon-entropy drop heuristic in code (rego has no log builtin) as a
# belt-and-suspenders net for high-entropy values that slip the key regex.

package openclaw.redaction

import future.keywords

# Key-name patterns that always mean "secret" → drop. Token-bounded so
# "author"/"description" don't match bare "auth"/etc.
secret_key_pattern := `(?i)(^|[_-])(api[_-]?key|secret|client[_-]?secret|access[_-]?key|token|password|passwd|authorization|cookie|bearer|private[_-]?key|refresh[_-]?token|session)([_-]|$)`

# Subject-prefix → set of payload field names that must be encrypted.
encrypt_fields_for_subject(field) if {
	startswith(input.subject, "oc.event.mail.")
	field in {"body", "subject", "snippet"}
}

encrypt_fields_for_subject(field) if {
	startswith(input.subject, "oc.event.calendar.")
	field in {"description", "summary", "location"}
}

encrypt_fields_for_subject(field) if {
	startswith(input.subject, "oc.mcp.fs-asher.")
	field in {"contents", "content", "diff"}
}

# Per-field decision. One decision per top-level payload key (object.keys
# so falsy values like "", 0, false are still classified).
decisions[field] := classify(field) if {
	some field in object.keys(input.payload)
}

classify(field) := "drop" if {
	regex.match(secret_key_pattern, field)
}

classify(field) := "encrypt" if {
	not regex.match(secret_key_pattern, field)
	encrypt_fields_for_subject(field)
}

classify(field) := "keep" if {
	not regex.match(secret_key_pattern, field)
	not encrypt_fields_for_subject(field)
}
