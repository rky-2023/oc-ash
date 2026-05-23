# notifier/

Outbound notification fan-out.

**Channels:**
- `notifier/fcm/` — Android via Firebase Cloud Messaging. Payloads are E2E-encrypted (Curve25519); FCM sees only `{cipher, nonce}`.

**Planned channels (Phase 8+):**
- email (transactional, via Postmark or self-hosted SMTP)
- Telegram
- Discord (low priority)

**Rules engine:** `policy/notify.rego` decides `{deliver, channel, priority}` for each event. Inputs: event type, repo, severity, time-of-day, DND schedule.

**Design rule:** the notifier is a pure subscriber of `oc.notify.<channel>.>`. Adding a channel = new subscriber, zero core changes.
