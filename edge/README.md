# edge/

Network edge configuration.

**Components:**
- **Caddy** — TLS termination, WAF rules, rate limiting, WebAuthn challenge multiplexer.
- **WireGuard** — point-to-point tunnel between the Android device (and rky's laptop) and the server. No public ingress except the GitHub webhook stub and FCM relay.
- **Cloudflare Tunnel** (planned) — hides origin IP for the small public surface.

Only the GitHub webhook receiver and the FCM relay are reachable from the public internet. Everything else lives behind WireGuard.

Layout (planned):
```
edge/
  Caddyfile
  wireguard/
    server.conf.tmpl
    peers/                  (per-device confs; secrets pulled from Vault)
  cloudflared/
    config.yml
```
