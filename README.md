# codex-idle-compact

[![CI](https://github.com/Winfred666/codex-idle-compact/actions/workflows/ci.yml/badge.svg)](https://github.com/Winfred666/codex-idle-compact/actions/workflows/ci.yml)

**Status: observer preview. Automatic compaction is unavailable on the audited Codex runtimes.**

The intended behavior is native compaction after a completed turn, at least **40% active context**, and **20 minutes of inactivity**. Research found a blocking race: Codex's `thread/compact/start` can abort an active turn, and accepts no atomic idle precondition. Checking status just before calling it cannot guarantee that new work is safe.

This release therefore ships a working, fail-closed local companion: a runtime diagnostic and an idle observer for an existing App Server connection. **It never initiates compaction.** There is no unsafe enable switch. See [verified findings](docs/research.md) and [architecture](docs/architecture.md).

## Install

Requires Python 3.9+, Linux or macOS, and an installed Codex CLI. One runtime dependency (`websockets`) handles the native Unix-socket transport. No database, service, API key, or model calls.

```sh
pipx install git+https://github.com/Winfred666/codex-idle-compact.git
codex-idle-compact doctor
```

Or run the diagnostic directly, with no package installation (the proxy requires `pip install .`):

```sh
git clone https://github.com/Winfred666/codex-idle-compact.git
cd codex-idle-compact
python3 codex_idle_compact.py doctor
```

`doctor` generates the installed binary's protocol schema in a temporary directory. It prints the version, compaction request fields, and limitation; **exit code 2 is expected** because safe automatic compaction is unsupported. It does not start a server or access conversation history. Unknown versions remain unsupported until their semantics have been reviewed.

## Observe an existing server

For an App Server client that lets you configure its stdio server command, use:

```sh
codex-idle-compact proxy \
  --socket /absolute/path/to/owning-app-server.sock \
  --idle-minutes 20
```

Configuration example for such a client's process launcher:

```json
{
  "command": "codex-idle-compact",
  "args": ["proxy", "--socket", "/absolute/path/to/owning-app-server.sock", "--idle-minutes", "50"]
}
```

This JSON describes a process launch, **not a Codex `config.toml` setting**. Use `60` or any positive finite number of minutes instead of `50`. `doctor --codex /path/to/codex` selects the binary to inspect.

The socket must already belong to the server owning the target threads. The companion connects directly using Codex's native WebSocket-over-Unix-socket protocol. It launches no subprocess while observing. It never runs `thread/start`, `thread/resume`, or a new App Server itself. Your client retains responsibility for initializing its connection and subscribing to its own threads. The companion preserves JSON payloads and translates only transport framing between stdio JSONL and WebSocket messages.

The usual shared-daemon socket is `$CODEX_HOME/app-server-control/app-server-control.sock` (`~/.codex` is the default home); long paths may be relocated by Codex. Use the actual owner socket. Do not start another server to load a desktop chat for this purpose.

**This is not a drop-in installation for Codex Desktop or the stock TUI.** It observes only the threads already subscribed on the connection routed through it. An already-running stdio-only desktop session cannot be attached this way. Hooks cannot supply the missing atomic operation, so this preview does not install a plugin, hooks, or global configuration.

For SSH, install and run the companion on the runtime host, with that host's owner socket. A configurable local client can launch it through `ssh host codex-idle-compact proxy --socket ...`. Do not expose an unauthenticated network listener.

## What the observer does

- Requires a newly observed `turn/started`, a successful `turn/completed`, and explicit `idle` status. Historical replay alone never arms it.
- Uses `tokenUsage.last.totalTokens / tokenUsage.modelContextWindow`; lifetime `total` is ignored. Cached input and reasoning tokens are not added again. Missing, negative, noninteger, inconsistent, or over-window data suppresses eligibility.
- Cancels on observed client activity, new turns, active/error/unloaded status, tool starts, queue changes, or native compaction. Closure/archive/deletion forgets the thread. Every thread has separate state.
- Rechecks its latest observed status and usage on expiry. It logs one `blocked` report per eligible completed turn, with a cooldown equal to the configured idle duration. A timer over 60 seconds late logs `skipped_late`.
- Uses an OS lock to prevent duplicate observers for one endpoint. Disconnecting discards all timers. Restarting waits for a fresh turn.

Example diagnostic on stderr, never on the protocol stdout:

```json
{"event":"blocked","threadId":"…","turnId":"…","usedTokens":40000,"contextWindow":100000,"idleSeconds":1200,"compactionStarted":false,"reason":"Automatic compaction is unavailable: the audited native API can replace an active turn and has no atomic idle/queue precondition."}
```

The preview does **not** claim that observation proves safety: it does not obtain an atomic snapshot of queues, background terminals, all clients, unsent typing, or pending automatic work. It never proceeds to a compaction request. It does not claim completion/failure monitoring for compactions it cannot safely initiate.

Prompt-cache expiry is not exposed here. Twenty minutes is a policy default, not a measured TTL or guaranteed cost saving. Native compaction is lossy and may consume tokens if a future safe implementation becomes available. Existing Codex auto-compaction settings remain untouched.

## Develop

```sh
python3 -m pip install .
python3 -m unittest discover -s tests -v
codex-idle-compact --version
```

Tests cover threshold boundaries, ambiguous usage, event order, activity cancellation, tool execution, queues, duplicate events, cooldown/rearming, compaction suppression, multiple threads, process locks, and payload-preserving relay. CI runs on Linux/macOS with Python 3.9/3.13.

Optional real transport smoke test (starts an empty isolated App Server, creates **no threads**, makes **no model calls**):

```sh
CODEX_IDLE_COMPACT_TEST_CODEX="$(command -v codex)" \
  python3 -m unittest discover -s tests -v
```

Audited locally on 2026-10-24: CLI **0.162.1**, desktop-bundled protocol **0.164.0-alpha.11.1**, and upstream source pinned in the research notes. Automatic compaction is intentionally unimplemented until the safety gap is closed. MIT licensed.
