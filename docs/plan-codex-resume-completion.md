# Codex Resume Completion Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the exact completed Codex response after a retired thread is
resumed, without duplicating normally buffered turns.

**Architecture:** Preserve the final response from Codex's completion hook in
the local daemon signal. Handle each signal as setup-then-completion, and use the
hook response only to complete or recover the watcher buffer for that turn.
Reattachment performs an explicit active-room title transition rather than
depending on branch-change detection. That transition is best-effort decoration
and cannot gate watcher attachment or completion delivery.

**Tech Stack:** Python 3.12, asyncio, unittest/pytest, Codex notify hooks, Matrix
bridge daemon.

## Global Constraints

- Follow `docs/design-codex-resume-completion.md` exactly.
- Write regression tests before production changes and observe the intended
  failure before implementation.
- Reuse the existing session and Matrix room; do not change authentication or
  room creation semantics.
- A reused room must attempt to restore its active title even when its saved
  branch equals the current branch. Matrix rejection or failure must warn and
  continue without advancing saved title metadata.
- The payload final is a fallback/completion guard, not a replacement for normal
  buffered delivery.
- Keep existing turn-ID deduplication and send the recovered final with
  `notify_final=True`.
- Do not implement the separate per-event signal queue in this change.
- The implementation agent runs no git commands; the orchestrator owns all git
  state and commits.
- Current `origin/main` baseline is 63 passing and 2 unrelated failures in
  `tests/test_tts_routing.py`; this change must introduce no additional full-suite
  failures.

---

### Task 1: Recover completion after a retired session resumes

**Files:**

- Modify: `packages/codex-matrix/src/codex_matrix/notify_handler.py`
- Modify: `packages/codex-matrix/src/codex_matrix/daemon.py`
- Modify: `packages/codex-matrix/src/codex_matrix/bridge.py`
- Modify: `tests/test_codex_daemon.py`
- Create or modify: `tests/test_codex_notify_handler.py`

**Interfaces:**

- Consumes: Codex `agent-turn-complete` payload field
  `last-assistant-message: str`.
- Produces: daemon signal field `last_assistant_message: str` and a testable
  setup-then-completion handler.
- Produces: an explicit bridge operation that applies `STATUS_ACTIVE` to an
  existing session room, reports Matrix success, and synchronizes its saved
  branch only after success.
- Extends: `_on_turn_complete(thread_id, turn_id, fallback_assistant)` while
  preserving existing callers through a default empty fallback.

- [ ] **Step 1: Write the failing daemon regression test**

  Model an inactive existing session that the notification handler has
  reactivated, an empty watched-session set, and an empty pending-assistant
  buffer. Process a completion signal containing `thread_id`, `turn_id`, `cwd`,
  `tmux_pane`, and `last_assistant_message`. Assert that setup occurs before
  completion, the existing room is retained, its active title is restored even
  when the branch is unchanged, the exact fallback is sent once with
  `notify_final=True`, and a duplicate callback for the same turn sends nothing.
  Use a non-empty completed transcript and assert the watcher initializes at its
  end-of-file offset, so the fallback—not transcript replay—causes delivery.

- [ ] **Step 2: Write the failing notify serialization test**

  Invoke `handle_notify` with a temporary state directory and a payload whose
  `last-assistant-message` is distinctive. Stub daemon startup and session-file
  classification. Assert that the written signal preserves that text under the
  daemon's snake-case field name.

- [ ] **Step 3: Prove the tests fail for the intended missing behavior**

  Run:

  ```bash
  uv run pytest tests/test_codex_daemon.py tests/test_codex_notify_handler.py -q
  ```

  Expected: the new tests fail because the signal omits the final response and
  the setup branch does not process completion.

- [ ] **Step 4: Carry the final response through the notify signal**

  In `handle_notify`, copy `payload.get("last-assistant-message", "")` to the
  signal as `last_assistant_message`. Preserve all current validation,
  registration, unmirrored-session filtering, and daemon startup behavior.

- [ ] **Step 5: Make signal handling setup-then-completion**

  Extract the body of the signal loop into one async method that accepts the
  decoded signal. If the thread is not watched, call `_setup_session`; after that
  call `_on_turn_complete` for every valid thread, passing the turn ID and final
  assistant fallback.

- [ ] **Step 6: Restore the active room title during reattachment**

  Add an explicit bridge lifecycle operation symmetric with
  `mark_session_ended`. It must build and apply `STATUS_ACTIVE` using the current
  branch, return whether Matrix accepted the rename, and update the saved branch
  only after success. After `_setup_session` attaches a reused room and completion
  delivery finishes, queue the active-title attempt without awaiting cosmetic
  I/O. Cold-start discovery must queue the same attempt after watcher attachment.
  Do not rely on `refresh_branch_if_changed`, whose unchanged-branch fast path
  cannot clear an ended marker. The daemon must warn on a false result or
  exception so cosmetic state never gates attachment or delivery. Keep at most
  one active-title task per session; a failed attachment queues none, and
  retirement must remove the session from the watched set and join any in-flight
  active task before applying the ended title. Do not cancel a title request
  already sent to Matrix, because cancelling the local await does not prove the
  homeserver will not apply it later. Serialize active restoration, branch
  refresh, and the ended transition with one per-session title lock. Retirement
  waits behind any in-flight branch refresh, applies the ended title last, and
  marks the session inactive before releasing the lock. Preserve cancellation
  of all decoration tasks during daemon shutdown, where no competing ended
  transition is emitted.

- [ ] **Step 7: Complete or recover the buffered assistant response**

  Extend `_on_turn_complete` with a default-empty fallback parameter. When
  buffered messages exist, append the fallback only if the last buffered
  assistant text differs. When no buffered assistant exists, send the non-empty
  fallback as the sole assistant message. Preserve hidden-automation recovery as
  the final fallback when neither buffered nor hook text exists, and preserve
  in-flight plus completed-turn deduplication.

- [ ] **Step 7a: Keep branch decoration outside transcript delivery**

  Buffer and deliver a file batch, including any task-complete control, before
  awaiting branch-title refresh. Make branch refresh report Matrix rejection,
  advance saved branch metadata only after acknowledgement, and warn without
  aborting delivery on a false result or exception. Cover the notify/file
  interleaving that previously could strand a completed assistant response in
  the next turn's buffer.

- [ ] **Step 8: Prove the focused tests pass**

  Run:

  ```bash
  uv run pytest tests/test_codex_daemon.py tests/test_codex_notify_handler.py -q
  ```

  Expected: all focused tests pass with no warnings or errors.

- [ ] **Step 9: Run the complete suite and compare with baseline**

  Run:

  ```bash
  uv run pytest tests/ -q
  ```

  Expected: no failures beyond the two pre-existing
  `tests/test_tts_routing.py` baseline failures; all new and existing Codex tests
  pass.

- [ ] **Step 10: Orchestrator checkpoint commit**

  After independently inspecting the diff and verification output, the
  orchestrator commits the implementation and tests with a descriptive bug-fix
  message. The implementation agent must not run git.
