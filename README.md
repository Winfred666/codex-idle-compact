# codex-idle-compact

[![CI](https://github.com/Winfred666/codex-idle-compact/actions/workflows/ci.yml/badge.svg)](https://github.com/Winfred666/codex-idle-compact/actions/workflows/ci.yml)

A small Codex plugin that **automatically runs native compaction after 20 minutes idle**, when the latest completed turn uses **at least 40% of the effective context window**.

**Enabled when installed.** Trusted lifecycle hooks start one local watcher automatically. No separate enable command, background-service setup, API key, or pip install. Supports Linux and macOS, including installation on an SSH runtime host. Requires `python3` 3.9+ and a daemon-backed Codex session with an accessible owning Unix socket. Tested with Codex CLI 0.162.1.

## Install

```sh
codex plugin marketplace add Winfred666/codex-idle-compact
codex plugin add codex-idle-compact@codex-idle-compact
```

Complete Codex's normal hook trust review if prompted. Start your next session normally; existing sessions may need a new session to load the hooks. **Installation does not restart anything.** A standard TUI using the shared local daemon is supported; `--no-daemon` / embedded mode and stdio-only desktop sessions cannot be attached through this plugin. Unsupported sessions receive a SessionStart hint.

For SSH, run these commands on the machine running Codex, with that runtime's `CODEX_HOME`. The plugin stays on that host and uses its local socket.

## Configuration

Defaults work without a configuration file. To change the inactivity interval, save this as `$CODEX_HOME/codex-idle-compact.json` (normally `~/.codex/codex-idle-compact.json`):

```json
{"idleMinutes": 50}
```

Use `60`, `20`, or another positive number of minutes. Changes are picked up automatically. Threshold stays at 40%; cooldown equals the configured idle interval.

The default owner endpoint is `$CODEX_HOME/app-server-control/app-server-control.sock`. For a runtime using another Unix socket, add `"socket": "/absolute/path/to/owner.sock"`. The hook verifies the kernel-reported socket owner is an ancestor of its process; merely pointing at another running daemon does not work. The plugin never starts a server to load an existing chat.

Uninstall with:

```sh
codex plugin remove codex-idle-compact@codex-idle-compact
```

## What runs

1. A trusted hook records only session/turn identifiers and activity. It starts one watcher, protected by an OS process lock.
2. The watcher attaches to the same loaded session on its verified owning daemon. A matching `Stop` hook, successful `turn/completed`, and native `idle` status are all required.
3. It calculates `tokenUsage.last.totalTokens / tokenUsage.modelContextWindow`. Lifetime totals are ignored; cached/reasoning tokens are not added twice. Missing, inconsistent, or ambiguous usage disarms the timer.
4. Submitted user input, new turns, tools, queue changes, compaction, session closure, or disconnect cancel eligibility. Native/manual compaction never rearms itself; a new normal completed turn is required.
5. At expiry, it rechecks usage, inactivity, latest turn, status, queued work, background terminals, goals, and active descendants. If eligible, it calls **`thread/compact/start`** and waits for the corresponding `contextCompaction` item **and** successful turn completion. An empty RPC acknowledgment is not success. Unknown outcomes are not automatically retried.

Each session has separate state, a single-flight lock, and a cooldown. Sleep or a delayed watcher loop cancels pending timers. The watcher exits after its sessions disappear. Runtime files and a small rotating `watcher.log` live in Codex's `PLUGIN_DATA` directory; they contain identifiers and event names, not prompts or transcripts. One small BSD-licensed WebSocket wheel is bundled for installation without pip; application code is MIT.

## Limits

- **Idle checks are best effort.** Codex's public compact request has no atomic “only if still idle” guard. Input arriving between the final check and the request can be interrupted by native compaction. Unsubmitted composer typing is not exposed by hooks. This plugin cannot promise zero interference with active users; [research](docs/research.md) describes the exact API limitation.
- Subscription uses `thread/resume` only after verifying the owner and an already-loaded thread. There is no public atomic loaded-only subscribe operation; an unload in that narrow gap is also possible. No separate Codex server/session is launched by the plugin.
- Hook trust is required. Hosts without a reachable owning Unix endpoint are unsupported. Some TUI launch flags force embedded mode. Custom/very long home paths may require an explicit socket setting. Unknown required API responses skip compaction.
- Prompt-cache expiry is not observable. Twenty minutes is a policy, not a TTL guarantee or a promise of savings. Native compaction can consume tokens and loses detail. Existing Codex automatic-compaction settings are untouched; a nonzero `model_post_turn_compact_threshold_percent` may still compact immediately, independently of this plugin.
- Version 0.1 was an incomplete observer. **Version 0.2 implements the trigger and automatic hook startup.** It chooses practical idle checks while explicitly retaining the native race above.

## Develop and verify

```sh
python3 -m pip install .
python3 -m unittest discover -s tests -v
```

Run the native integration test against an installed binary:

```sh
CODEX_IDLE_COMPACT_TEST_CODEX="$(command -v codex)" \
  python3 -m unittest discover -s tests -v
```

The native test installs the actual plugin into a disposable home, trusts only that test fixture, creates a synthetic thread on an isolated server, and serves canned responses locally. It verifies automatic native compaction on that same server/thread, completion, and no repeat. No external inference or API key. CI tests Linux/macOS and Python 3.9/3.13.

For socket/config diagnostics from a checkout: `python3 codex_idle_compact.py doctor`. A reachable socket is not proof that hooks in a particular session have been trusted or loaded. See [architecture](docs/architecture.md), [version-pinned research](docs/research.md), and [third-party license](vendor/LICENSE.websockets).
