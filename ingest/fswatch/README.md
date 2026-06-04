# oc-fswatch — filesystem-watch ingester (Phase 3 task 3.1)

A thin Rust producer that watches `/home/asher` recursively and POSTs coalesced
filesystem events to the **oc-ingest worker** over its unix socket. The worker
classifies each event as `oc.event.fs.<repo>.<kind>`, filters it through
`policy/ingest.rego`, signs `sig_service`, and anchors it in immudb like every
other event.

It is a *producer only*: it never talks to NATS or Vault. (The original phase-3
draft had each ingester publish to NATS directly; the accepted "shared ingest
service + thin producer" architecture routes all sources through the one worker
socket. Standalone-service split → Phase 11, ADR-003 D3.)

## What it does

- Watches `OC_FSWATCH_ROOT` (default `/home/asher`) recursively via `notify`.
- Drops high-churn noise locally via `exclude.toml` (`.git/objects`,
  `node_modules`, `target`, `*.pyc`, our own `sessions/`, …). `ingest.rego`
  applies a second server-side cut (dist/build/coverage/`*.log`).
- Coalesces bursts per path inside a debounce window (`OC_FSWATCH_DEBOUNCE_MS`,
  default 1000): 50 writes to one file in 1 s → one event with `change_count: 50`.
- Maps `Create→created`, `Modify→modified`, `Remove→deleted`; a rename emits a
  `deleted`(old) + `created`(new) pair sharing a `rename_pair` correlation id.
- `<repo>` is the immediate child of the root containing the path; loose files
  directly under the root use `_root`.

## Payload

```json
{
  "source": "fswatch",
  "path": "/home/asher/openclaw/PLAN.md",
  "repo": "openclaw",
  "kind": "modified",
  "size_bytes": 12847,
  "mtime": "2026-06-04T14:02:11.341+00:00",
  "change_count": 1
}
```

## Build

```sh
cargo build --release    # needs a C linker (cc/gcc); cargo check does not
cargo test               # pure-logic unit tests (repo mapping, exclude globs)
```

> **Build prerequisite:** linking the binary needs a system C toolchain
> (`cc`/`gcc`). `cargo check` and `cargo test` for the pure helpers compile
> without one, but `cargo build` will fail at the link step until `gcc` (or
> `build-essential`) is installed.

## Run (dev)

```sh
OC_INGEST_SOCKET=/tmp/oc-ingest.sock \
OC_FSWATCH_EXCLUDE=ingest/fswatch/exclude.toml \
  ./target/release/oc-fswatch
```

Then `touch /home/asher/openclaw/SMOKE.md` → an `oc.event.fs.openclaw.created`
appears in the audit viewer within ~debounce + projection time.

## Production

Install as the dedicated `oc-fswatch` user via `oc-fswatch.service` (see the
header of that file). Hardened: `ProtectSystem=strict`, `ReadOnlyPaths=/home/asher`,
`NoNewPrivileges`, `RestrictAddressFamilies=AF_UNIX`.

## Config

| Env | Default | Meaning |
|---|---|---|
| `OC_INGEST_SOCKET` | `/run/openclaw/ingest.sock` | worker socket to POST to |
| `OC_FSWATCH_ROOT` | `/home/asher` | directory watched recursively |
| `OC_FSWATCH_EXCLUDE` | `exclude.toml` | path to the exclusion globs |
| `OC_FSWATCH_DEBOUNCE_MS` | `1000` | per-path coalescing window |
