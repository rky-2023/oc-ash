# ADR-004: Public ingress — defer-and-poll now, Cloudflare Tunnel when triggered

- **Status:** Accepted
- **Date:** 2026-05-23
- **Deciders:** rky
- **Supersedes:** —
- **Superseded by:** —
- **Related:** ADR-001 R4 (Tailscale-now-Cloudflare-later, webhook ingestion deferred), Phase 3 runbook (event ingestion), `docs/THREAT_MODEL.md` (attack surface)

---

## Context

openclaw is designed as a single-user system reachable only over Tailscale. ADR-001 R4 locked that as the network-edge stance — *no public ingress except possibly one webhook endpoint*. The question this ADR resolves is: how do we get GitHub webhook events into openclaw if openclaw has no public address?

Three things are true and create the tension:

1. GitHub webhooks are *outbound from github.com → some public HTTPS endpoint*. GitHub cannot ride your tailnet to reach you; their outbound IPs are public and your endpoint must be public-reachable.
2. We want zero public attack surface during early phases — every extra public service is one more thing to harden against T1 (random internet scanning).
3. We do want some signal from GitHub (PR opened, CI status, push to a watched repo) in Phase 3+.

The viable options are:

- **A. Defer-and-poll** — no public ingress. Phase 3 ingests GitHub events by *polling* the GitHub API on a cadence using the `openclaw-bot` App credentials.
- **B. Tailscale Funnel** — expose a single endpoint to the public internet *via Tailscale's coordination plane*. Adds Tailscale itself to the trusted public-ingress path.
- **C. Cloudflare Tunnel (cloudflared)** — Cloudflare's outbound-only tunnel: openclaw initiates a TLS connection to Cloudflare's edge; Cloudflare receives webhook traffic on a public hostname and tunnels it to openclaw. No port forwarding; origin IP hidden.
- **D. Direct expose** — open port 443 on your router, run Caddy with HSTS + WAF rules + Crowdsec/fail2ban. Standard but maximum surface.

This ADR picks A for now and pre-commits to C as the future trigger.

---

## Decision

### D1. Current state: defer-and-poll.

For Phase 0, Phase 1, Phase 2, and Phase 3, openclaw exposes **zero** public ingress. GitHub events are ingested by a **polling worker** (`ingest/gh-poller/`) that runs inside the trust boundary and uses `openclaw-bot` App credentials.

Polling cadence:
- **PRs / Issues / Reviews:** 60 s (each watched repo).
- **Workflow / Check Runs:** 30 s (so CI status changes feel reasonably live).
- **Pushes / Branch updates:** 60 s.
- **Releases / Tags:** 5 min.

Backoff on GitHub 429s: exponential, capped at 10 min. Backoff state stored in `openclaw.lookup` (not in Vault — see ADR-002 D12).

The poller publishes to the same `oc.event.gh.>` NATS subjects that a webhook receiver would. Downstream consumers don't care which path delivered the message.

### D2. Future trigger to switch from A → C.

The switch happens when **any one** of these becomes true:

- Polling-driven latency is unacceptable for some user-facing flow. The most likely trigger is "I want a notification within 5 seconds when a PR comment lands" — polling can't do that economically.
- GitHub rate-limiting becomes painful — secondary rate limits apply to App polling and may bite when we're watching many repos at once. Inflection is around ~10 repos polled at 30 s, depending on activity.
- A second event source needs public ingress (e.g., a third-party service that *only* speaks webhooks). At that point, building the public path once and routing many webhooks through it amortizes the cost.

When the trigger fires, **Cloudflare Tunnel is the chosen path.** Not Tailscale Funnel, not direct expose.

### D3. Why Cloudflare Tunnel (when the trigger fires).

- **No router config.** `cloudflared` makes outbound connections; no port-forwarding, no UPnP, no public IP on the home connection.
- **Origin IP stays hidden.** Cloudflare's edge receives public traffic; your home IP never appears in DNS.
- **WAF + rate limit + bot detection** at Cloudflare's edge, ahead of openclaw. Free tier is enough for personal scale.
- **Cloudflare Access** can additionally gate the tunnel by identity (e.g., only Google-authenticated rky can reach an admin URL), giving us a defense-in-depth layer the GitHub-webhook path doesn't actually need but Phase 9's admin UI might.
- **Zero new long-running ports on the host.** `cloudflared` is one outbound process that can be sandboxed like any MCP server (Phase 10 hardening).

The downside is a new trust dependency on Cloudflare. We accept it because:
- Cloudflare's compromise scenario is "Cloudflare can MITM the tunneled traffic." We mitigate by signing webhooks at the application layer (HMAC validation in `gh-webhook` matches what GitHub sends; Cloudflare cannot forge a valid HMAC without GitHub's webhook secret, which it never sees).
- Most public-facing single-host deployments at this size end up on Cloudflare anyway; this is mainstream not exotic.

### D4. Why not Tailscale Funnel.

Tailscale Funnel exposes a tailnet service to the public internet via Tailscale's edge. It's simpler than Cloudflare Tunnel in some ways. We reject it for two reasons:

- It puts Tailscale's coordination plane on the public-ingress path. Tailscale is already the trusted internal-mesh provider; making it *also* the public-ingress provider concentrates risk in one vendor. If a Tailscale compromise affects both your internal mesh AND your public endpoint simultaneously, blast radius is larger than splitting the vendors.
- Tailscale Funnel has fewer WAF / bot-management primitives than Cloudflare. Adequate for one webhook endpoint, but underpowered if we ever need the public path for more than that.

Tailscale stays in its lane: internal mesh + ACLs + MagicDNS. Cloudflare gets the public path when we need one.

### D5. Why not direct expose with Caddy + WAF.

Direct expose works and is the most "you own everything" option. We reject it because:

- Port forwarding on a residential router is one config away from being broken on every router-firmware update.
- Origin IP becomes public — leaks one piece of OPSEC for no real gain.
- Crowdsec / fail2ban catch known bad actors but not novel ones; Cloudflare's edge has detection signals you can't replicate at small scale.

If for some reason Cloudflare becomes untenable (Cloudflare ceases free tier, you have ideological objections, etc.), direct expose with Caddy + WAF + Crowdsec is the fallback. Document the switch in a follow-up ADR if it happens.

### D6. What lives behind the public path when it exists.

When Cloudflare Tunnel is wired up (some future Phase X), the only thing behind it initially is the **GitHub webhook receiver** (`ingest/gh-webhook/`):

- Public hostname: `gh.<your-cloudflare-zone>` (or similar).
- TLS terminated at Cloudflare's edge.
- HMAC validation per GitHub's webhook secret (rotated weekly via Vault).
- IP allowlist of GitHub's published webhook source ranges (defense-in-depth; Cloudflare's WAF can do this rule).
- Audit-log every received payload before any business logic runs.

The polling worker can be retired at that point, or kept running as a backup ingestion path that catches webhooks GitHub failed to deliver. Default: retire the poller to simplify.

---

## Consequences

### Positive

- **Zero public ingress today.** Phases 0–3 close out without ever opening a port on the home router. T1 (unauth internet scanning) is fully resisted.
- **GitHub events still flow.** Latency is 30–60 s — fine for "audit-log of repo activity" purposes; not fine for "real-time chat-like reactions to PR comments," but we don't need that yet.
- **The future migration is fully specified.** When the trigger fires, no design work is needed; we already chose Cloudflare Tunnel. Phase 3 runbook can be marked "ready to wire" without ambiguity.

### Negative

- **Polling vs. webhook semantics differ.** Polling can miss events that occurred and were already mutated again before the next poll (e.g., a PR opened and closed within 30 s). For the audit-log use case, we get the *final* state of those events but miss the intermediate transitions. Acceptable for this threat model; documented.
- **Secondary rate limits on GitHub Apps** apply per-App per-hour. At normal personal scale (a few repos, polling every 30–60 s), this is fine. Beyond ~10 repos polled at 30 s, you may start hitting limits.
- **One more thing to migrate later.** When Cloudflare Tunnel arrives, we have to write the webhook receiver, configure the tunnel, and switch downstream consumers. The webhook receiver code is small and was already designed in PLAN.md Phase 3.

### Neutral

- We are committing to Cloudflare Tunnel as the *eventual* public-ingress path. If your Cloudflare account is ever compromised, an attacker could redirect your tunnel; mitigation is signing at the application layer (HMAC), which removes Cloudflare from the trust path for payload integrity.

---

## Alternatives considered

### Alt 1: Open everything to Tailscale, expose webhook via Tailscale Funnel.

Rejected per D4 — concentrates risk in one vendor; weaker WAF primitives.

### Alt 2: Direct expose with Caddy + WAF.

Rejected per D5 — leaks origin IP; weaker bot/edge detection than Cloudflare; residential router is fragile.

### Alt 3: Defer GitHub events entirely — don't ingest them at all.

Considered. Reject: PR/CI/repo activity is one of the primary things the system is supposed to surface (PLAN.md Phases 3 + 7); cutting that scope removes a core capability.

### Alt 4: Use GitHub's GraphQL API for webhook-equivalent events.

Considered. GraphQL doesn't help — webhooks vs. polling is an API-shape question, not a protocol-shape question. GraphQL polling has the same latency characteristics as REST polling.

### Alt 5: Cloudflare Workers as a webhook proxy that forwards to the tailnet via a deploy-key-authenticated push.

Over-engineered for a one-webhook problem. Reject; revisit if we ever need a public path for things that *can't* be HMAC-signed by the source.

---

## Open questions

- The exact "switch trigger" criteria in D2 are stated qualitatively. Quantify in a follow-up if/when we approach the threshold (e.g., "more than 8 repos polled, or >2 % poll failures").
- Whether to use Cloudflare Access for the admin UI in Phase 9 (separate from the webhook path). Defer to a future ADR.
- Whether to publish a public `_health.json` at the Cloudflare edge once the tunnel exists, so external monitoring can ping it. Low priority; defer.

---

## Change log

- **2026-05-23 (v1)** — Accepted as drafted. Locks defer-and-poll for current phases; pre-commits to Cloudflare Tunnel as the future-trigger choice.
