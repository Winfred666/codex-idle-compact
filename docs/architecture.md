# Minimal architecture

```text
Configured App Server client
        ⇅ JSON-RPC lines
Python observer (one event loop, thread-keyed in-memory state)
        ⇅ native WebSocket-over-Unix-socket protocol
Existing owning App Server / daemon
```

The companion owns no conversations and sends no additional RPCs. It does not inspect or modify transcripts. A per-endpoint `flock` prevents two companion instances; lock files contain no conversation data. Session state is keyed by `threadId`, not a potentially shared session-tree ID. This avoids mixing parent, fork, and subagent context.

The observer accepts usage only for its currently observed turn. It requires an explicit completed turn and idle status in either notification order. A duplicate completion cannot reset the deadline or rearm a canceled report. The last model request's total includes its input and output; cumulative turn/session totals are not a context measurement. The event's effective window is used directly without applying the context-window reserve twice.

Activity invalidates eligibility. Completion of a later turn is the only rearming path. Compaction items permanently exclude their turn, so native/manual compaction cannot recursively rearm the observer. Unknown JSON clears pending state while payloads are still relayed. EOF tears down the proxy and all timers; there is no reconnect or replay-based catch-up.

The timer currently ends at a **blocked report**. Adding a call after the current predicate would be unsafe. A working automatic version needs a native owner-side operation that atomically:

1. Requires a still-loaded thread, the expected completed turn/context revision, idle status, no tool execution, no queued/automatic work, and an acceptable activity timestamp.
2. Reserves compaction without replacing a turn that won the race; new input must be able to preempt background compaction safely.
3. Returns an identifiable compaction operation/turn whose context-compaction item, successful completion, error, interruption, and disconnect can be correlated.

A passive, loaded-only subscription would also avoid the attach/unload race of using `thread/resume` merely to observe another client's session. Unsent composer activity needs a client signal; lifecycle hooks only observe submitted prompts.

No speculative RPC name, undocumented guard parameter, or implementation hook is shipped. There is no model-assisted compaction fallback, terminal input injection, Codex fork, database, persistent daemon, or model-facing skill.
