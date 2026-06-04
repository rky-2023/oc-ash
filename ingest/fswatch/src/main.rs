//! oc-fswatch — openclaw filesystem-watch ingester (Phase 3 task 3.1).
//!
//! Watches `/home/asher` recursively via the `notify` crate (inotify under the
//! hood on Linux), applies an exclusion list, coalesces bursts per path inside
//! a debounce window, and POSTs each resulting event to the **oc-ingest unix
//! socket** as `{"source":"fswatch", ...}`.
//!
//! Design note (deviation from the original phase-3 draft): the draft had each
//! ingester publish to NATS directly with its own mTLS identity. The accepted
//! "shared ingest service + thin producer" architecture instead routes every
//! source (git hooks, Claude hooks, gh-poller, fswatch) through the one
//! oc-ingest worker over its unix socket, which filters → signs sig_service →
//! publishes to JetStream. fswatch is therefore a thin Rust producer; it never
//! touches NATS or Vault. The standalone-service split is Phase 11 (ADR-003 D3).
//!
//! The worker classifies our payload as `oc.event.fs.<repo>.<kind>`, runs it
//! through `policy/ingest.rego` (which drops dist/build/coverage/*.log noise
//! server-side), and anchors it in immudb like every other event.
//!
//! Configuration (all optional, env-overridable):
//!   OC_INGEST_SOCKET   unix socket to POST to   (default /run/openclaw/ingest.sock)
//!   OC_FSWATCH_ROOT    directory to watch        (default /home/asher)
//!   OC_FSWATCH_EXCLUDE path to exclude.toml      (default ./exclude.toml)
//!   OC_FSWATCH_DEBOUNCE_MS  coalescing window ms (default 1000)

use std::collections::HashMap;
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Component, Path, PathBuf};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use glob::{MatchOptions, Pattern};
use notify::event::{ModifyKind, RenameMode};
use notify::{EventKind, RecursiveMode, Watcher};
use serde::{Deserialize, Serialize};

const DEFAULT_SOCKET: &str = "/run/openclaw/ingest.sock";
const DEFAULT_ROOT: &str = "/home/asher";
const DEFAULT_EXCLUDE: &str = "exclude.toml";
const DEFAULT_DEBOUNCE_MS: u64 = 1000;

/// exclude.toml shape: a list of glob patterns (`**` crosses separators).
#[derive(Deserialize, Default)]
struct ExcludeConfig {
    #[serde(default)]
    exclude: Vec<String>,
}

/// The JSON body POSTed to the oc-ingest socket. Mirrors `build_fs_event` in
/// core/app/ingest/hooks.py.
#[derive(Serialize)]
struct FsEvent {
    source: &'static str,
    path: String,
    repo: String,
    kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    size_bytes: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mtime: Option<String>,
    change_count: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    rename_pair: Option<String>,
}

/// A path with pending (debounced) activity. `kind` is the most recent mapped
/// kind; `count` is how many raw events coalesced into it.
struct Pending {
    kind: &'static str,
    count: u64,
    last: Instant,
    rename_pair: Option<String>,
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

/// Compute `<repo>`: the immediate child directory of `root` that contains the
/// path. Loose files directly under `root` (and `root` itself) map to `_root`.
fn repo_of(path: &Path, root: &Path) -> String {
    let rel = match path.strip_prefix(root) {
        Ok(r) => r,
        Err(_) => return "_root".to_string(),
    };
    let comps: Vec<Component> = rel.components().collect();
    // >=2 components means first component is a sub-directory (the repo);
    // a single component is a loose file/dir sitting directly under root.
    if comps.len() >= 2 {
        if let Some(Component::Normal(c)) = comps.first() {
            return c.to_string_lossy().into_owned();
        }
    }
    "_root".to_string()
}

/// Map a notify event to (path, kind, rename_pair) tuples. Renames become a
/// deleted(old) + created(new) pair sharing a correlation id; everything else
/// is a single entry.
fn map_event(
    ev: &notify::Event,
    rename_counter: &mut u64,
) -> Vec<(PathBuf, &'static str, Option<String>)> {
    match ev.kind {
        EventKind::Create(_) => ev.paths.iter().map(|p| (p.clone(), "created", None)).collect(),
        EventKind::Remove(_) => ev.paths.iter().map(|p| (p.clone(), "deleted", None)).collect(),
        EventKind::Modify(ModifyKind::Name(RenameMode::Both)) if ev.paths.len() == 2 => {
            *rename_counter += 1;
            let id = format!("rn-{}", rename_counter);
            vec![
                (ev.paths[0].clone(), "deleted", Some(id.clone())),
                (ev.paths[1].clone(), "created", Some(id)),
            ]
        }
        EventKind::Modify(ModifyKind::Name(RenameMode::From)) => {
            ev.paths.iter().map(|p| (p.clone(), "deleted", None)).collect()
        }
        EventKind::Modify(ModifyKind::Name(RenameMode::To)) => {
            ev.paths.iter().map(|p| (p.clone(), "created", None)).collect()
        }
        EventKind::Modify(_) => ev.paths.iter().map(|p| (p.clone(), "modified", None)).collect(),
        _ => vec![],
    }
}

fn is_excluded(path: &Path, patterns: &[Pattern], opts: &MatchOptions) -> bool {
    let s = path.to_string_lossy();
    patterns.iter().any(|p| p.matches_with(&s, *opts))
}

/// Best-effort POST to the oc-ingest unix socket. A down/slow worker must never
/// stall the watcher, so failures are logged and swallowed.
fn post_event(socket: &str, ev: &FsEvent) {
    let body = match serde_json::to_string(ev) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("oc-fswatch: serialize failed: {e}");
            return;
        }
    };
    if let Err(e) = post_body(socket, &body) {
        eprintln!("oc-fswatch: post to {socket} failed: {e}");
    }
}

fn post_body(socket: &str, body: &str) -> std::io::Result<()> {
    let mut stream = UnixStream::connect(socket)?;
    stream.set_write_timeout(Some(Duration::from_secs(3)))?;
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    let req = format!(
        "POST /hook HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream.write_all(req.as_bytes())?;
    stream.flush()?;
    // Drain the response so the server completes its write before we close.
    let mut buf = [0u8; 64];
    let _ = stream.read(&mut buf);
    Ok(())
}

/// Build and POST the coalesced event for one path. Stats the file for
/// size/mtime (skipped for deletes, where the path is gone).
fn flush_one(socket: &str, root: &Path, path: &Path, p: &Pending) {
    let (mut size_bytes, mut mtime) = (None, None);
    if p.kind != "deleted" {
        if let Ok(meta) = std::fs::metadata(path) {
            size_bytes = Some(meta.len());
            if let Ok(modified) = meta.modified() {
                let dt: DateTime<Utc> = modified.into();
                mtime = Some(dt.to_rfc3339());
            }
        }
    }
    let ev = FsEvent {
        source: "fswatch",
        path: path.to_string_lossy().into_owned(),
        repo: repo_of(path, root),
        kind: p.kind,
        size_bytes,
        mtime,
        change_count: p.count,
        rename_pair: p.rename_pair.clone(),
    };
    post_event(socket, &ev);
}

fn load_excludes(path: &str) -> Vec<Pattern> {
    let raw = match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("oc-fswatch: no exclude config at {path} ({e}); watching everything");
            return Vec::new();
        }
    };
    let cfg: ExcludeConfig = toml::from_str(&raw).unwrap_or_else(|e| {
        eprintln!("oc-fswatch: malformed {path}: {e}; watching everything");
        ExcludeConfig::default()
    });
    expand_globs(&cfg.exclude)
        .iter()
        .filter_map(|g| match Pattern::new(g) {
            Ok(p) => Some(p),
            Err(e) => {
                eprintln!("oc-fswatch: bad glob {g:?}: {e}");
                None
            }
        })
        .collect()
}

/// For every `.../<dir>/**` pattern, also emit the bare `.../<dir>` form. A
/// `/**` glob matches a directory's *contents* but not the directory node
/// itself (its own create/delete event), so we silence both.
fn expand_globs(globs: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(globs.len());
    for g in globs {
        out.push(g.clone());
        if let Some(prefix) = g.strip_suffix("/**") {
            out.push(prefix.to_string());
        }
    }
    out
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let socket = env_or("OC_INGEST_SOCKET", DEFAULT_SOCKET);
    let root_str = env_or("OC_FSWATCH_ROOT", DEFAULT_ROOT);
    let exclude_path = env_or("OC_FSWATCH_EXCLUDE", DEFAULT_EXCLUDE);
    let debounce = Duration::from_millis(
        env_or("OC_FSWATCH_DEBOUNCE_MS", &DEFAULT_DEBOUNCE_MS.to_string())
            .parse()
            .unwrap_or(DEFAULT_DEBOUNCE_MS),
    );
    let root = PathBuf::from(&root_str);
    let patterns = load_excludes(&exclude_path);
    // `**` must cross path separators; `*` must not.
    let opts = MatchOptions { require_literal_separator: true, ..MatchOptions::new() };

    eprintln!(
        "oc-fswatch: watching {} → {} (debounce {}ms, {} exclude globs)",
        root.display(),
        socket,
        debounce.as_millis(),
        patterns.len()
    );

    let (tx, rx) = channel();
    let mut watcher = notify::recommended_watcher(move |res| {
        let _ = tx.send(res);
    })?;
    watcher.watch(&root, RecursiveMode::Recursive)?;

    let mut pending: HashMap<PathBuf, Pending> = HashMap::new();
    let mut rename_counter: u64 = 0;

    loop {
        // Wake at least every debounce window so idle paths get flushed.
        match rx.recv_timeout(debounce) {
            Ok(Ok(event)) => {
                for (path, kind, rename_pair) in map_event(&event, &mut rename_counter) {
                    if is_excluded(&path, &patterns, &opts) {
                        continue;
                    }
                    pending
                        .entry(path)
                        .and_modify(|p| {
                            p.kind = kind;
                            p.count += 1;
                            p.last = Instant::now();
                            if rename_pair.is_some() {
                                p.rename_pair = rename_pair.clone();
                            }
                        })
                        .or_insert(Pending {
                            kind,
                            count: 1,
                            last: Instant::now(),
                            rename_pair,
                        });
                }
            }
            Ok(Err(e)) => eprintln!("oc-fswatch: watch error: {e}"),
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }

        // Flush paths idle for at least the debounce window.
        let now = Instant::now();
        let ready: Vec<PathBuf> = pending
            .iter()
            .filter(|(_, p)| now.duration_since(p.last) >= debounce)
            .map(|(k, _)| k.clone())
            .collect();
        for path in ready {
            if let Some(p) = pending.remove(&path) {
                flush_one(&socket, &root, &path, &p);
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repo_is_immediate_child() {
        let root = Path::new("/home/asher");
        assert_eq!(repo_of(Path::new("/home/asher/openclaw/PLAN.md"), root), "openclaw");
        assert_eq!(
            repo_of(Path::new("/home/asher/ashboard-backend/src/x.ts"), root),
            "ashboard-backend"
        );
    }

    #[test]
    fn loose_files_and_root_map_to_root() {
        let root = Path::new("/home/asher");
        assert_eq!(repo_of(Path::new("/home/asher/loose.txt"), root), "_root");
        assert_eq!(repo_of(Path::new("/home/asher"), root), "_root");
        assert_eq!(repo_of(Path::new("/etc/passwd"), root), "_root");
    }

    #[test]
    fn excludes_match_across_separators() {
        let opts = MatchOptions { require_literal_separator: true, ..MatchOptions::new() };
        let pats = vec![
            Pattern::new("**/node_modules/**").unwrap(),
            Pattern::new("**/*.pyc").unwrap(),
        ];
        assert!(is_excluded(
            Path::new("/home/asher/openclaw/node_modules/x/y.js"),
            &pats,
            &opts
        ));
        assert!(is_excluded(Path::new("/home/asher/a/b/c.pyc"), &pats, &opts));
        assert!(!is_excluded(Path::new("/home/asher/openclaw/PLAN.md"), &pats, &opts));
    }

    #[test]
    fn expand_globs_adds_bare_dir_form() {
        let out = expand_globs(&[
            "**/node_modules/**".to_string(),
            "**/*.pyc".to_string(),
        ]);
        // contents form + bare-dir form for the /** pattern; *.pyc untouched.
        assert!(out.contains(&"**/node_modules/**".to_string()));
        assert!(out.contains(&"**/node_modules".to_string()));
        assert!(out.contains(&"**/*.pyc".to_string()));
        assert_eq!(out.len(), 3);

        // The bare-dir glob matches the directory node itself.
        let opts = MatchOptions { require_literal_separator: true, ..MatchOptions::new() };
        let pats: Vec<Pattern> = out.iter().map(|g| Pattern::new(g).unwrap()).collect();
        assert!(is_excluded(Path::new("/home/asher/x/node_modules"), &pats, &opts));
    }
}
