# Minimal architecture

```text
Codex lifecycle hooks → private activity signals → one Python watcher
                                                    ⇅ native WebSocket / Unix
                                             same owning App Server
                                                    ↓
                                             thread/compact/start
```

The hook checks the socket's kernel-reported peer PID against its ancestors. The watcher verifies that PID again on connection, initializes JSON-RPC, reads the already-loaded thread, and subscribes with `thread/resume`. It never invokes `thread/start`, starts Codex, reads a transcript, or injects UI input. Its subscription requests just the most recent turn's metadata and receives native usage/events. No database or network service is introduced.

`SessionStart` enrolls a session; `Stop` authorizes eligibility for its specific turn. `UserPromptSubmit`, `Interrupt`, `PreCompact`, `PostCompact` and `SessionEnd` invalidate it. A hook exits promptly after an atomic signal-file replacement; it never sleeps for the inactivity interval. Multiple events can collapse to the newest signal, conservatively skipping an opportunity. Native turn and activity events independently invalidate eligibility. Subagent turn IDs cannot authorize their parent's completed turn; separately identified child threads are not enrolled.

The timer starts at a successful completion, with a matching Stop and effective usage. It uses the last response footprint of the current turn, not accumulated steps or lifetime usage. A late initial subscription starts its interval at observation, never backdates it. State is keyed by thread ID and verified owner, with an instance lock, per-thread in-flight lock, cooldown and one attempt per completed turn. New work is the only way to rearm. Timers are discarded on disconnection or sleep; there is no catch-up scan of old conversation files.

Preflight reads cover the queue, background terminals, active goals, descendants, latest turn and status. Hook activity and native event revisions are checked again immediately before sending the native compact request. Any failed read, busy state, pagination ambiguity or activity change skips that opportunity. A sent request requires a matching context-compaction item and completed turn; failure, timeout or disconnect cannot trigger a blind retry.

**These checks are not atomic.** The public compact operation can replace a turn that begins between check and send. `thread/resume` also has no loaded-only precondition. Replace these with native guarded operations when available; do not infer stronger guarantees from the external locks. Unsubmitted typing is outside the available event stream.

Code: `idle_plugin.py` handles hooks/config/ownership and `codex_idle_compact.py` handles subscriptions, policy and native compaction. Bundled WebSocket code handles protocol framing; no custom protocol implementation. Plugin removal deletes its cached source, causing the watcher to exit; disabling hooks prevents fresh Stop authorizations, but an already-armed timer can remain until canceled or expired.
