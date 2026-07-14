# Reliable Codex Completion Delivery After Session Resume

## Status

Approved for implementation on 2026-07-14.

## Problem

The Codex bridge can lose a completed assistant response when a previously
retired Codex thread is resumed.

The observed sequence was:

1. Cleanup retired an inactive tmux-backed session, preserving its Matrix room
   mapping but clearing its pending assistant buffer and watched-session flag.
2. The same Codex thread resumed later. Watchdog consumed the new transcript
   bytes, but the daemon discarded their messages while the session-map entry
   remained inactive.
3. Codex emitted an `agent-turn-complete` notification containing the thread,
   turn, and complete `last-assistant-message`.
4. The notification handler reactivated the session. The daemon took its setup
   branch, attached the watcher at the transcript's current end, and skipped the
   mutually exclusive completion branch.
5. The completed response was behind the new watcher offset, the pending buffer
   was empty, and normal sessions had no recovery source.
6. Reactivation preserved the room's previous branch metadata, so the ordinary
   branch-refresh path treated its red ended title as current and did not restore
   the active room name.

This is a lifecycle race, not a Matrix authentication or room-routing failure.

## Decision

Treat every Codex completion notification as both an attachment signal and a
completion signal.

The notify handler will carry Codex's authoritative
`last-assistant-message` through the existing local daemon signal. The daemon
will first attach or refresh the session when necessary and then always process
the completion event. The transcript watcher remains the primary source of
buffered progress and assistant output; the notification text is a turn-bound
fallback when the watcher did not retain the final response.

The existing turn identifier remains the deduplication key. If the watcher and
notify hook both report the same completion, only one Matrix delivery occurs.

Reattaching an existing room will also perform an explicit lifecycle transition
back to its active title. This operation is distinct from branch-change refresh:
it always removes the ended marker even when the branch has not changed.

## Completion Flow

1. `handle_notify` validates the Codex payload, reactivates the existing session
   mapping, and writes the thread ID, turn ID, cwd, tmux pane, and final assistant
   text to the local signal.
2. The daemon receives the signal. If the thread is not currently watched, it
   attaches the existing transcript, reuses the existing Matrix room, and
   explicitly restores that room's active name.
3. The daemon processes the completion regardless of whether attachment was
   necessary.
4. If buffered assistant messages exist, they are delivered normally. If their
   final item is not the notification's final response, the notification text is
   appended so partial watcher delivery cannot notify on commentary instead of
   the completed answer.
5. If no assistant messages are buffered, the notification text is delivered as
   the recovery response.
6. The completion is recorded by turn ID before a duplicate callback can produce
   a second delivery.

## Failure Handling and Security

An absent final-message field preserves current behavior; it does not invent or
replay an older response. The fallback text is already present in Codex's local
transcript and is carried only through the existing local state directory before
being sent to the session's existing Matrix room. No credential handling or room
authorization changes are introduced.

## Tests

The regression test models an existing session and Matrix room that were
retired, then reactivated in a resumed pane with no pending assistant buffer. A
completion signal with a turn ID and final response must attach the watcher,
reuse the room, send the exact response once with `notify_final=True`, record the
turn, and ignore a duplicate callback.

A notify-handler test proves that `last-assistant-message` survives signal
serialization. Existing turn-completion tests continue to prove that buffered
delivery and hidden automation behavior are unchanged.

The resume regression also starts with a red ended room whose saved branch is
unchanged. It proves that setup invokes the explicit active-title transition
instead of the branch-change-only refresh.

## Alternatives Rejected

Extending the tmux-pane cleanup grace period merely delays the failure and does
not cover resumes after retirement. Reconstructing a completed turn from the
entire JSONL transcript adds a turn-aware parser and relies on evolving Codex
transcript ordering even though the completion hook already supplies the exact
final response. A broader lifecycle refactor that persists watcher cursors could
restore all progress output across detach and resume, but it is unnecessary for
reliable final-response delivery.

## Out of Scope

The daemon currently uses one overwriteable signal file, so simultaneous
top-level completions can race. That did not cause this incident and will be
handled separately with an atomic per-event queue. This change also does not
alter session cleanup policy or replay historical progress messages.

## Acceptance Criteria

- A resumed, previously retired Codex thread delivers the completion response to
  its existing Matrix room.
- The reused room loses its red ended marker even when its branch is unchanged.
- The response is sent exactly once and remains eligible for server-side voice.
- Ordinary watched sessions retain their current buffered delivery behavior.
- Missing fallback text does not replay an unrelated prior assistant response.
- Duplicate transcript and notify callbacks for one turn do not duplicate the
  Matrix message.
