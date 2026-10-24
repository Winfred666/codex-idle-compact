# Research and verified limitations

Inspected 2026-10-24 before implementation. Release source: [`rust-v0.162.1`, commit `7a078f6`](https://github.com/openai/codex/tree/7a078f6bb4ce0dc2a81408b334ce52979d92c603). Also checked upstream [`970111f`](https://github.com/openai/codex/tree/970111f6cb62cc02fe36a82ea7d71b37a2889c97). Findings are version-specific, not a promise about future releases.

## Why automatic compaction is blocked

The released [`ThreadCompactStartParams`](https://github.com/openai/codex/blob/7a078f6bb4ce0dc2a81408b334ce52979d92c603/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1164) contains only `thread_id`. The [request handler](https://github.com/openai/codex/blob/7a078f6bb4ce0dc2a81408b334ce52979d92c603/codex-rs/app-server/src/request_processors/thread_processor.rs#L2368) looks up a loaded thread and submits `Op::Compact`. The [core handler](https://github.com/openai/codex/blob/7a078f6bb4ce0dc2a81408b334ce52979d92c603/codex-rs/core/src/session/handlers.rs#L247) calls `abort_all_tasks(TurnAbortReason::Replaced)` before starting the compaction task.

Thus this interleaving is possible:

```text
watcher: thread/read → idle
client:  turn/start → begins new work
watcher: thread/compact/start → replaces that work
```

An external lock, another status read, a Stop hook, or a PreCompact hook cannot make those separate operations atomic. Request serialization orders requests; it does not convert this operation into compare-and-swap or reject-if-busy. The request returns `{}` before compaction completes, so an acknowledgment alone would also be insufficient proof of success.

Generated schemas from installed CLI **0.162.1** and desktop binary **0.164.0-alpha.11.1** both expose only `threadId`. The exact alpha implementation was not independently source-audited; its schema supplies no safety guarantee. No destructive race was executed against a user's session.

## Native setting

`model_post_turn_compact_threshold_percent` is validated from 0 to 100 and defaults to 0 in [release configuration](https://github.com/openai/codex/blob/7a078f6bb4ce0dc2a81408b334ce52979d92c603/codex-rs/core/src/config/mod.rs#L4317). The [turn loop](https://github.com/openai/codex/blob/7a078f6bb4ce0dc2a81408b334ce52979d92c603/codex-rs/core/src/session/turn.rs#L719) checks the threshold and pending input at the end of a response and performs post-turn compaction inside the running task. It is not an inactivity timer. Setting it to 40 would not implement this project's goal.

The public [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) did not list this key when inspected; the release source is the evidence for it. The companion never changes this or other user settings. If already enabled, native immediate/post-turn compaction can still occur independently.

## Hooks and plugin packaging

Official [hooks documentation](https://learn.chatgpt.com/docs/hooks) and [plugin packaging](https://developers.openai.com/plugins/build/plugins) support bundled command hooks, including:

| Hook | Relevant meaning / limit |
| --- | --- |
| `Stop` | Runs before the turn's lifecycle is fully over; other hooks can request continuation. It is not proof that the session is idle. |
| `UserPromptSubmit` | Can invalidate an idle timer on submitted input. Does not expose unsubmitted typing. |
| `SessionEnd` | Can clear session state. Does not give a live owner RPC handle. |
| `PreCompact` | Runs on a compaction path; cannot undo the earlier replacement of an active task. |
| `PostCompact` | Can report compaction lifecycle; cannot initiate a guarded idle compaction. |

Common hook input supplies a session identifier, cwd, model and an unstable transcript path; it does not supply a public atomic idle-compaction operation. Subagent hooks may share the parent session ID. Sleeping inside a synchronous Stop hook delays completion; returning a continuation instruction incurs a new model turn. Neither solves this request.

Root `plugin.json` / compatibility `.codex-plugin/plugin.json` and `hooks/hooks.json` packaging are supported, with user trust required for hook execution. Packaging is not the missing capability. This release ships a local companion rather than installing nonfunctional compaction hooks.

## Daemon, usage and event APIs

Official [App Server documentation](https://learn.chatgpt.com/docs/app-server) describes `thread/tokenUsage/updated`, `thread/status/changed`, `thread/loaded/list`, `thread/read`, and `thread/compact/start`, with progress through turn events and `contextCompaction` items. The installed schema also exposes `thread/queue/list`, `thread/queue/changed`, and `thread/backgroundTerminals/list`. These separate reads cannot form an atomic safety gate.

[`ThreadTokenUsage`](https://github.com/openai/codex/blob/7a078f6bb4ce0dc2a81408b334ce52979d92c603/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L1934) maps core lifetime usage to `total` and the last model response footprint to `last`. The [turn context](https://github.com/openai/codex/blob/7a078f6bb4ce0dc2a81408b334ce52979d92c603/codex-rs/core/src/session/turn_context.rs#L756) supplies the usable/effective context window. The observer uses the last response within the latest completed turn, not a sum of model steps, cached tokens, or lifetime totals. It is a reported footprint, not an exact recount of arbitrary subsequent history mutations.

Local CLI help confirms `codex app-server daemon`, `codex app-server proxy --sock`, and Unix/WebSocket transports. The native proxy is a raw byte tunnel, so its stdin requires a WebSocket handshake, not plain JSONL. A real transport test caught this distinction; the companion uses `websockets.unix_connect` directly. Both attach to an existing owner; launching another App Server and resuming the same stored ID does not establish ownership of the original live thread. `thread/read` does not subscribe, while `thread/resume` may load a thread; there is no documented loaded-only passive subscription with an ownership precondition in the inspected surface.

On the inspected workstation, `codex app-server daemon version` reported no shared control socket; the active desktop server used stdio. The existing desktop session was therefore not accessible through the public daemon socket. Its transport and configuration were not modified.

## Related implementations

- [`davidar/claude-idle-compact`, `35ec925`](https://github.com/davidar/claude-idle-compact/tree/35ec925345acfce26bc0ba260c1f611dceab1148): a Claude mods implementation using `turn.complete`, `turn.start`, `$.clock.after`, and `$.session.compact`. Its native runtime integration is not a Codex hook API. Its README describes a 50-minute policy for an assumed one-hour Claude cache; that assumption is not transferred here.
- [`liekkasfc/codex-native-auto-compact`, `0e7c6e8`](https://github.com/liekkasfc/codex-native-auto-compact/tree/0e7c6e8246561d23e5a7448b02fb7d34f406d49d): a userscript that inspects UI/network state and triggers buttons or slash-menu commands via DOM/input events. Those UI mechanisms violate this project's native-API-only requirement. No implementation code was copied.

## Cache timing and verification scope

The [official prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching) documents model- and retention-dependent behavior. It does not establish an exact expiry deadline for a particular Codex session. A timer is only a user-selected policy; 20, 50 and 60 minutes are supported by the observer, with no promise that a cache remains warm.

Unit tests exercise the policy with synthetic protocol events. The opt-in native smoke test checks a real existing-server proxy connection and confirms zero loaded threads; it creates no conversations and issues no inference requests. No end-to-end native automatic compaction or cache saving has been verified, because the required safe operation is absent. The product goal remains blocked; the shipped functionality is diagnostics and observation only.
