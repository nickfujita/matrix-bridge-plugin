"""Codex Matrix daemon — bidirectional bridge between Codex and Matrix.

Combines:
1. Matrix /sync polling for inbound messages (Matrix → tmux)
2. JSONL file watching for outbound messages (Codex session → Matrix)

The daemon discovers Codex sessions via the notify hook signal file.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from matrix_bridge.config import load_config, MatrixConfig
from matrix_bridge.matrix import MatrixClient
from matrix_bridge.session import SessionMap
from matrix_bridge.tmux import pane_current_command, pane_for_open_file, send_keys

from .bridge import CodexBridge
from .daemon_lifecycle import remove_stale_or_owned_pid_record, write_daemon_identity
from .transcript import (
    extract_latest_assistant_after_last_hidden_marker,
    extract_session_meta,
    find_session_file,
    format_turn_error,
    has_hidden_user_marker,
    is_interactive_session,
    is_unmirrored_session,
)
from .watcher import SessionWatcher

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ccmatrix"
CODEX_ACTIVE_COMMANDS = {"codex", "node"}
SESSION_CLEANUP_INTERVAL_SECONDS = 30
UNKNOWN_PANE_STALE_SECONDS = 10 * 60

# How long a *running* turn may add nothing to its rollout before the room gets
# a state line. Turn-complete events cannot answer "did the agent stop?" — by
# construction they only arrive when it did not. This is the only signal that
# can.
#
# The threshold has to clear the longest legitimate quiet stretch or the notice
# is worse than useless. A single `wait_agent` call has been measured at 60
# minutes of zero rollout growth on a perfectly healthy session, so 90 minutes
# is the floor that keeps this quiet in normal operation. It posts one m.notice
# (silent, no push) and re-arms only once the rollout moves again.
STALL_NOTICE_SECONDS = 90 * 60

# Cap the repair burst on the first pass after upgrade. Every session retired
# before room_marked_ended existed reads as needing a rename, and on a
# long-lived machine that is dozens of rooms at once.
ROOM_RECONCILE_MAX_PER_PASS = 20


class CodexDaemon:
    """Bidirectional Codex ↔ Matrix bridge daemon."""

    def __init__(self, config: MatrixConfig):
        self.config = config
        self.bridge = CodexBridge(config)
        # Separate Matrix client for inbound polling (uses bot token)
        self.poll_client = MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url)
        self.session_map = SessionMap(STATE_DIR / "codex-sessions.json")
        self.watcher: SessionWatcher | None = None
        self.running = True
        self.next_batch: str | None = None
        # Track which sessions we're actively watching
        self.watched_sessions: set[str] = set()
        self._reset_runtime_state()

    def _reset_runtime_state(self) -> None:
        """Initialize the daemon's in-memory bookkeeping.

        Split out from __init__ so tests that assemble a partial daemon get all
        of it from one call. Adding a field here previously meant editing every
        such test builder, and forgetting one failed as an AttributeError deep
        inside an unrelated code path.
        """
        # Buffer assistant messages until turn-complete to avoid notification spam
        self._pending_assistant: dict[str, list[dict]] = {}
        # Dedupe turn-complete handling when both transcript and notify fire.
        self._last_completed_turn: dict[str, str] = {}
        self._inflight_turns: set[tuple[str, str]] = set()
        # Room-title updates are cosmetic. Keep strong references to their
        # background tasks so they cannot delay watcher attachment or delivery.
        self._decoration_tasks: set[asyncio.Task[None]] = set()
        self._active_title_tasks: dict[str, asyncio.Task[None]] = {}
        self._title_locks: dict[str, asyncio.Lock] = {}
        # Serializes "does this session have a room yet, and if not make one".
        # Room creation awaits, so two callers can otherwise both observe a
        # missing room_id and both create one.
        self._room_locks: dict[str, asyncio.Lock] = {}
        # Sessions with a turn in flight (task_started seen, no task_complete
        # or turn_aborted yet), and the stall notices already posted for them.
        self._active_turns: set[str] = set()
        self._stall_notified: dict[str, float] = {}
        self._runtime_tasks: set[asyncio.Task] = set()
        self._start_task: asyncio.Task | None = None

    def request_shutdown(self) -> None:
        """Stop accepting work and wake all long-running runtime loops."""
        self.running = False
        if self._start_task is not None:
            self._start_task.cancel()
        for task in list(self._runtime_tasks):
            task.cancel()

    async def start(self) -> None:
        """Start the daemon — runs both inbound and outbound loops."""
        self._start_task = asyncio.current_task()
        try:
            if not self.running:
                return
            try:
                async with self.bridge:
                    async with self.poll_client:
                        try:
                            loop = asyncio.get_event_loop()
                            self.watcher = SessionWatcher(self._on_file_messages)
                            self.watcher.start(loop)

                            # Discover any existing active sessions
                            await self._discover_sessions()

                            if not self.running:
                                return

                            runtime_tasks = {
                                asyncio.create_task(self._matrix_poll_loop()),
                                asyncio.create_task(self._signal_watch_loop()),
                                asyncio.create_task(self._session_cleanup_loop()),
                            }
                            self._runtime_tasks.update(runtime_tasks)
                            await asyncio.gather(*runtime_tasks)
                        finally:
                            runtime_tasks = list(self._runtime_tasks)
                            for task in runtime_tasks:
                                task.cancel()
                            if runtime_tasks:
                                await asyncio.gather(*runtime_tasks, return_exceptions=True)
                            self._runtime_tasks.clear()
                            tasks = list(self._decoration_tasks)
                            for task in tasks:
                                task.cancel()
                            if tasks:
                                await asyncio.gather(*tasks, return_exceptions=True)
                            self._active_title_tasks.clear()
                            if self.watcher:
                                self.watcher.stop()
            except asyncio.CancelledError:
                # request_shutdown cancels this task so discovery, context
                # entry, Matrix long-polling, and cleanup sleep all unwind.
                # A caller cancellation while the daemon is still running is
                # a distinct request and must remain visible to that caller.
                if self.running:
                    raise
        finally:
            self._start_task = None

    async def _discover_sessions(self) -> None:
        """Find and watch any active sessions on startup."""
        for entry in self.session_map.active_sessions():
            session_file = find_session_file(entry.session_id)
            if session_file and is_unmirrored_session(session_file):
                await self._retire_session(entry.session_id, "unmirrored background thread")
                continue
            if session_file and entry.session_id not in self.watched_sessions:
                # Create Matrix room if session was registered but never got one
                reused_room = bool(entry.room_id)
                if not reused_room:
                    await self._ensure_room(entry.session_id, entry.cwd)
                self.watcher.watch_file(session_file)
                self.watched_sessions.add(entry.session_id)
                logger.info(f"Resumed watching session {entry.session_id[:8]}")
                if reused_room:
                    self._schedule_active_title_restore(entry.session_id)

    async def _restore_active_title(self, session_id: str) -> None:
        """Best-effort active-title decoration outside the delivery path."""
        async with self._title_lock(session_id):
            entry = self.session_map.get(session_id)
            if (
                not entry
                or not entry.active
                or not entry.room_id
                or session_id not in self.watched_sessions
            ):
                return
            try:
                title_restored = await self.bridge.mark_session_active(session_id)
            except Exception:
                logger.warning(
                    "Failed to restore active room title for %s; session delivery is unaffected",
                    session_id[:8],
                    exc_info=True,
                )
            else:
                if not title_restored:
                    logger.warning(
                        "Failed to restore active room title for %s; session delivery is unaffected",
                        session_id[:8],
                    )

    def _room_lock(self, session_id: str) -> asyncio.Lock:
        """Return the lock serializing room creation for a session."""
        lock = self._room_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._room_locks[session_id] = lock
        return lock

    async def _ensure_room(self, session_id: str, cwd: str) -> None:
        """Create the session's Matrix room, exactly once.

        Three call sites can reach room creation for the same session — startup
        discovery, the notify signal and the file watcher — and they interleave
        freely because `create_room` awaits a Matrix round-trip. Without this
        lock the second caller re-reads a session map that the first has not
        written back yet, sees no room_id, and creates a duplicate; the map then
        keeps only the last one and the earlier room is orphaned, holding a
        fragment of the session's mirror stream. Re-checking the map *inside*
        the lock is what makes the map entry the source of truth: the loser of
        the race reuses the winner's room instead of creating its own.
        """
        async with self._room_lock(session_id):
            entry = self.session_map.get(session_id)
            if entry and entry.room_id:
                return
            await self.bridge.create_room(session_id, cwd)

    async def _reactivate_session(self, session_id: str, entry, reason: str) -> None:
        """Bring a retired session back, keeping the room it already owns.

        Retirement used to be a one-way door. Every later event for the session
        hit an `active` guard and returned, so a single false positive from the
        staleness heuristic cost the entire remainder of the session — 14 hours
        and ~13 MB of rollout in the incident this was written for. Fresh
        qualifying activity is proof the session is alive, and `register`
        preserves `room_id`, so the operator's existing room simply resumes
        rather than a second one appearing beside it.
        """
        # Passing the stored pane keeps a real tmux binding intact; `register`
        # ignores the "unknown" placeholder rather than overwriting with it.
        self.session_map.register(session_id, entry.tmux_pane, entry.cwd)
        self.watched_sessions.add(session_id)
        logger.info(f"Un-retired Codex session {session_id[:8]}: {reason}")
        if entry.room_id:
            # The room is still titled with the ended marker. Put it back.
            self._schedule_active_title_restore(session_id)

    def _title_lock(self, session_id: str) -> asyncio.Lock:
        """Return the lock serializing every title transition for a session."""
        lock = self._title_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._title_locks[session_id] = lock
        return lock

    def _schedule_active_title_restore(self, session_id: str) -> asyncio.Task[None]:
        """Schedule at most one active-title restoration for a session."""
        existing = self._active_title_tasks.get(session_id)
        if existing and not existing.done():
            return existing

        task = asyncio.create_task(self._restore_active_title(session_id))
        self._decoration_tasks.add(task)
        self._active_title_tasks[session_id] = task

        def discard(completed: asyncio.Task[None]) -> None:
            self._decoration_tasks.discard(completed)
            if self._active_title_tasks.get(session_id) is completed:
                self._active_title_tasks.pop(session_id, None)

        task.add_done_callback(discard)
        return task

    async def _refresh_branch_title(self, session_id: str) -> None:
        """Refresh branch decoration after delivery, warning on any failure."""
        async with self._title_lock(session_id):
            try:
                refreshed = await self.bridge.refresh_branch_if_changed(session_id)
            except Exception:
                logger.warning(
                    "Failed to refresh branch room title for %s; message delivery is unaffected",
                    session_id[:8],
                    exc_info=True,
                )
            else:
                if refreshed is False:
                    logger.warning(
                        "Failed to refresh branch room title for %s; message delivery is unaffected",
                        session_id[:8],
                    )

    async def _signal_watch_loop(self) -> None:
        """Watch for notify hook signals to discover new sessions and flush turns.

        The notify hook fires on agent-turn-complete. For new sessions, we set up
        watching and create a Matrix room. For existing sessions, we flush the
        buffered assistant messages (the final one is tagged cc.tts for
        server-side voice), only on turn completion, not on every intermediate
        file write.
        """
        signal_file = STATE_DIR / "codex-notify-signal"

        while self.running:
            try:
                if signal_file.exists():
                    import json
                    data = json.loads(signal_file.read_text())
                    signal_file.unlink(missing_ok=True)
                    await self._handle_notify_signal(data)
            except Exception as e:
                logger.error(f"Signal watch error: {e}")

            await asyncio.sleep(2)

    async def _handle_notify_signal(self, data: dict) -> None:
        """Attach a notified session if needed, then process its completion."""
        thread_id = data.get("thread_id", "")
        if not thread_id:
            return

        restore_existing_room = False
        if thread_id not in self.watched_sessions:
            entry = self.session_map.get(thread_id)
            restore_existing_room = bool(entry and entry.room_id)
            await self._setup_session(thread_id, data.get("cwd", ""), data.get("tmux_pane", ""))

        try:
            await self._on_turn_complete(
                thread_id,
                data.get("turn_id", ""),
                data.get("last_assistant_message", ""),
            )
        finally:
            if restore_existing_room and thread_id in self.watched_sessions:
                entry = self.session_map.get(thread_id)
                if entry and entry.active and entry.room_id:
                    self._schedule_active_title_restore(thread_id)

    async def _on_turn_complete(
        self,
        thread_id: str,
        turn_id: str = "",
        fallback_assistant: str = "",
        turn_error: str | None = None,
    ) -> None:
        """Flush buffered assistant messages when a turn completes.

        Assistant messages are buffered during file watching (sent silently as
        m.notice for progress).  On turn-complete we send them with
        notify_final=True so the last one triggers a push notification and is
        tagged cc.tts for server-side voice synthesis.
        """
        inflight_key: tuple[str, str] | None = None
        if turn_id:
            last_turn_id = self._last_completed_turn.get(thread_id)
            if last_turn_id == turn_id:
                logger.info(f"Skipping duplicate turn-complete for {thread_id[:8]} ({turn_id[:8]})")
                return
            inflight_key = (thread_id, turn_id)
            if inflight_key in self._inflight_turns:
                logger.info(f"Skipping in-flight duplicate turn-complete for {thread_id[:8]} ({turn_id[:8]})")
                return
            # Mark the turn as in-flight before any awaits. Transcript-based
            # completion and notify-hook completion can arrive almost
            # simultaneously; if we wait until after Matrix/TTS work finishes,
            # the second callback can synthesize a duplicate audio clip.
            self._inflight_turns.add(inflight_key)

        try:
            entry = self.session_map.get(thread_id)
            if not entry or not entry.room_id or not entry.active:
                return

            session_file = find_session_file(thread_id)
            if session_file and is_unmirrored_session(session_file):
                await self._retire_session(thread_id, "unmirrored background thread")
                return

            if turn_error:
                # The turn died. Anything buffered is mid-turn progress, and
                # flushing it as the final reply reports forward motion on a
                # session that has stopped — the operator would have been told
                # "the re-reviews are running now" about a turn a policy filter
                # had just killed. Replace it rather than adding to it, and
                # notify: this is the message that turns a silent night into a
                # phone buzz.
                self._pending_assistant.pop(thread_id, None)
                await self.bridge.send_messages(
                    thread_id,
                    [{"role": "assistant", "text": turn_error}],
                    notify_final=True,
                )
                await self.bridge.set_typing(thread_id, False)
                logger.info(f"Mirrored turn error for {thread_id[:8]}: {turn_error.splitlines()[0]}")
                if turn_id:
                    self._last_completed_turn[thread_id] = turn_id
                return

            # Flush buffered assistant messages — last one triggers notification
            pending = self._pending_assistant.pop(thread_id, [])
            final_only = bool(session_file and has_hidden_user_marker(session_file))
            if fallback_assistant and (
                not pending or pending[-1].get("text") != fallback_assistant
            ):
                pending.append({"role": "assistant", "text": fallback_assistant})
            logger.info(
                "Handling turn-complete for %s (turn=%s, pending=%d)",
                thread_id[:8],
                turn_id[:8] if turn_id else "missing",
                len(pending),
            )
            if pending:
                to_send = [pending[-1]] if final_only else pending
                await self.bridge.send_messages(thread_id, to_send, notify_final=True)
                logger.info(f"Flushed {len(to_send)} assistant msgs for {thread_id[:8]} with notification")
            elif final_only and session_file:
                recovered_text = extract_latest_assistant_after_last_hidden_marker(session_file)
                if recovered_text:
                    await self.bridge.send_messages(
                        thread_id,
                        [{"role": "assistant", "text": recovered_text}],
                        notify_final=True,
                    )
                    pending = [{"role": "assistant", "text": recovered_text}]
                    logger.info(f"Recovered final assistant msg for {thread_id[:8]} with notification")

            await self.bridge.set_typing(thread_id, False)

            # Voice is now server-side: the final assistant message sent above
            # carries the cc.tts tag, so no local synthesis happens here.

            if turn_id:
                self._last_completed_turn[thread_id] = turn_id
        finally:
            if inflight_key is not None:
                self._inflight_turns.discard(inflight_key)

    async def _setup_session(self, thread_id: str, cwd: str, tmux_pane: str) -> None:
        """Set up watching and Matrix room for a new Codex session."""
        session_file = find_session_file(thread_id)
        if not session_file:
            logger.warning(f"Session file not found for {thread_id[:8]}")
            return

        if is_unmirrored_session(session_file):
            await self._retire_session(thread_id, "unmirrored background thread")
            return

        # Extract metadata if not already registered
        entry = self.session_map.get(thread_id)
        if not entry:
            meta = extract_session_meta(session_file)
            if meta:
                cwd = meta.get("cwd", cwd)
            self.session_map.register(thread_id, tmux_pane, cwd)
        elif not entry.active:
            # The notify hook fired for a session the reaper had retired. The
            # unmirrored check above has already passed, so this session
            # qualifies for mirroring and the retirement was wrong.
            await self._reactivate_session(thread_id, entry, "notify hook fired")

        # Create Matrix room if needed
        entry = self.session_map.get(thread_id)
        if entry and not entry.room_id:
            await self._ensure_room(thread_id, entry.cwd or cwd)

        # Start watching the session file
        self.watcher.watch_file(session_file)
        self.watched_sessions.add(thread_id)
        logger.info(f"Now watching Codex session {thread_id[:8]}")

    async def _on_file_messages(self, path: Path, messages: list[dict]) -> None:
        """Called when new messages are detected in a session JSONL file.

        Identifies the session from the filename and forwards messages to Matrix.
        Auto-registers new sessions and creates Matrix rooms on first sight.
        """
        # Extract thread ID from filename: rollout-<timestamp>-<uuid>.jsonl
        filename = path.stem  # e.g. "rollout-2026-03-18T12-50-14-019d00ff-..."
        # The UUID is the last 36 chars of the stem
        thread_id = filename[-36:] if len(filename) >= 36 else ""

        if not thread_id:
            return

        if is_unmirrored_session(path):
            await self._retire_session(thread_id, "unmirrored background thread")
            return

        entry = self.session_map.get(thread_id)
        if entry and not entry.active:
            # New rollout content for a retired session. The unmirrored check
            # above has already passed, so this session still qualifies for
            # mirroring and is demonstrably alive — the file just grew. Bring
            # it back instead of discarding the rest of the session.
            await self._reactivate_session(thread_id, entry, "new rollout activity")
            entry = self.session_map.get(thread_id)

        # Auto-register new sessions discovered via file watcher
        if not entry:
            meta = extract_session_meta(path)
            cwd = meta.get("cwd", "") if meta else ""
            # The daemon's own environment is not tied to the originating
            # Codex pane. Register a provisional entry and let the notify hook
            # backfill the real pane when it fires.
            self.session_map.register(thread_id, "unknown", cwd)
            logger.info(f"Auto-registered Codex session {thread_id[:8]} from file watcher")
            entry = self.session_map.get(thread_id)

        # Create Matrix room if needed
        refresh_existing_room = bool(entry and entry.room_id)
        if entry and not entry.room_id:
            await self._ensure_room(thread_id, entry.cwd)
            entry = self.session_map.get(thread_id)

        if not entry or not entry.room_id or not entry.active:
            return

        control_events = [m for m in messages if m.get("role") == "control"]
        content_messages = [m for m in messages if m.get("role") != "control"]
        final_only = has_hidden_user_marker(path)

        # Split messages: send tool/user immediately (silent), buffer assistant
        # Assistant messages are held until turn-complete so only the final one
        # triggers a push notification (avoids notification spam on every batch).
        immediate = [] if final_only else [m for m in content_messages if m["role"] != "assistant"]
        assistant_msgs = [m for m in content_messages if m["role"] == "assistant"]

        if assistant_msgs:
            self._pending_assistant.setdefault(thread_id, []).extend(assistant_msgs)
            logger.info(f"Buffered {len(assistant_msgs)} assistant msgs from {thread_id[:8]}")

        if immediate:
            sent = await self.bridge.send_messages(thread_id, immediate)
            if sent > 0:
                logger.info(f"Forwarded {sent} messages from {thread_id[:8]} to Matrix")

        # Update typing indicator
        if assistant_msgs:
            # Assistant wrote something — clear typing, turn-complete will flush
            await self.bridge.set_typing(thread_id, False)
        elif immediate:
            # Still working (tool calls only) — show typing
            await self.bridge.set_typing(thread_id, True)

        seen_turn_ids: set[str] = set()
        for event in control_events:
            event_name = event.get("event")

            if event_name == "task_started":
                self._active_turns.add(thread_id)
                self._stall_notified.pop(thread_id, None)
                continue

            if event_name == "turn_aborted":
                self._active_turns.discard(thread_id)
                self._stall_notified.pop(thread_id, None)
                continue

            if event_name != "task_complete":
                continue

            self._active_turns.discard(thread_id)
            self._stall_notified.pop(thread_id, None)

            turn_id = event.get("turn_id", "")
            key = turn_id or "__missing__"
            if key in seen_turn_ids:
                continue
            seen_turn_ids.add(key)
            # The transcript is a second source of truth for turn completion.
            # Keep listening here so final reply delivery still works if the
            # notify hook is delayed or misses a callback.
            #
            # For an errored turn it is the *only* source: Codex does not fire
            # the notify hook at all when a turn ends in error, so nothing
            # reaches the daemon down the hook path.
            await self._on_turn_complete(
                thread_id, turn_id, turn_error=format_turn_error(event),
            )

        # Branch names are cosmetic. Refresh only after all messages and
        # completion controls from this file batch have been handled so a slow
        # or failed Matrix state event cannot lose or reorder assistant output.
        if refresh_existing_room:
            await self._refresh_branch_title(thread_id)

    async def _retire_session(self, session_id: str, reason: str) -> None:
        """Mark a session inactive and stop all outbound Matrix/TTS work."""
        entry = self.session_map.get(session_id)
        self._pending_assistant.pop(session_id, None)
        self.watched_sessions.discard(session_id)

        # An earlier resume may still have an asynchronous active-title request
        # in flight. Join it before emitting the ended title. Cancelling the
        # local await is insufficient because Matrix may still apply a request
        # that already reached the homeserver after the ended event.
        active_title_task = self._active_title_tasks.pop(session_id, None)
        if active_title_task and active_title_task is not asyncio.current_task():
            await asyncio.gather(active_title_task, return_exceptions=True)

        # Branch refreshes are awaited inline rather than tracked as background
        # decoration tasks. Serialize behind any in-flight refresh and keep the
        # session active until it finishes; the ended rename then runs last.
        async with self._title_lock(session_id):
            if entry and entry.room_id:
                try:
                    await self.bridge.mark_session_ended(session_id)
                except Exception as e:
                    logger.warning(f"Failed to mark Codex session {session_id[:8]} ended: {e}")

            # Mark inactive before releasing the title lock. Any queued refresh
            # will then observe an inactive session and perform no rename.
            self.session_map.deregister(session_id)
        logger.info(f"Retired Codex session {session_id[:8]}: {reason}")

    async def _session_cleanup_loop(self) -> None:
        """Mark Codex Matrix rooms inactive when their tmux pane is gone."""
        while self.running:
            await asyncio.sleep(SESSION_CLEANUP_INTERVAL_SECONDS)
            try:
                await self._cleanup_ended_sessions()
            except Exception as e:
                logger.error(f"Session cleanup error: {e}")
            try:
                await self._reconcile_room_status()
            except Exception as e:
                logger.error(f"Room status reconcile error: {e}")

    async def _reconcile_room_status(self) -> None:
        """Make every room title agree with its session's real state.

        Renaming used to happen only on the paths that call `_retire_session`,
        which left two holes and no way out of either:

        * Same-pane succession. `SessionMap.register` retires the previous
          session on a pane by writing the data layer directly — there is no
          Matrix client at that level, and it also runs inside the notify-hook
          subprocess. That is the *normal* way an interactive session ends, and
          it was the one common exit that never got a red dot.
        * Resumption. Nothing in the bridge ever renamed a room back *out* of
          the ended state, so a session that came back — including one the
          reaper retired by mistake — kept a room that said it was over.

        Comparing the two recorded facts catches both, and any future path that
        forgets to rename, without that path having to know about this one.
        Failures leave the flag untouched so the next pass retries.
        """
        repaired = 0
        for entry in self.session_map.all_sessions():
            if not entry.room_id:
                continue
            if entry.active == (not entry.room_marked_ended):
                continue
            if repaired >= ROOM_RECONCILE_MAX_PER_PASS:
                return

            async with self._title_lock(entry.session_id):
                # Re-read inside the lock: a retire or resume may have landed
                # while an earlier entry in this pass was awaiting Matrix.
                current = self.session_map.get(entry.session_id)
                if not current or not current.room_id:
                    continue
                if current.active == (not current.room_marked_ended):
                    continue

                if current.active:
                    ok = await self.bridge.mark_session_active(entry.session_id)
                    label = "active"
                else:
                    ok = await self.bridge.mark_session_ended(entry.session_id)
                    label = "ended"

            repaired += 1
            if ok:
                logger.info(
                    "Reconciled room title for %s → %s", entry.session_id[:8], label,
                )
            else:
                logger.warning(
                    "Failed to reconcile room title for %s → %s; will retry",
                    entry.session_id[:8], label,
                )

    async def _cleanup_ended_sessions(self) -> None:
        """Retire active sessions whose originating Codex pane has ended."""
        for entry in self.session_map.active_sessions():
            session_file = find_session_file(entry.session_id)

            if not entry.tmux_pane or entry.tmux_pane == "unknown":
                # The file watcher registers every session it discovers with a
                # placeholder pane, expecting the notify hook to backfill the
                # real one. When that backfill does not arrive the entry keeps
                # the placeholder indefinitely and the staleness heuristic below
                # eventually retires a live session. Rather than trusting the
                # hook, resolve the pane from the process that holds the rollout
                # open — the daemon can always do this for itself.
                if session_file:
                    pane = pane_for_open_file(session_file)
                    if pane:
                        self.session_map.register(entry.session_id, pane, entry.cwd)
                        logger.info(
                            "Backfilled tmux pane %s for Codex session %s",
                            pane, entry.session_id[:8],
                        )
                        entry = self.session_map.get(entry.session_id)
                        if not entry:
                            continue

            if not entry.tmux_pane or entry.tmux_pane == "unknown":
                # Still no pane. A session genuinely launched outside tmux is
                # legitimate, and for an interactive one the absence of a pane
                # says nothing about whether it is alive — so the staleness
                # heuristic must not apply to it. It stays aimed at the
                # background subagent and `codex exec` threads it was built for,
                # which do end silently and do need reaping.
                if session_file and is_interactive_session(session_file):
                    await self._maybe_notify_stall(entry, session_file)
                    continue
                if session_file and time.time() - session_file.stat().st_mtime > UNKNOWN_PANE_STALE_SECONDS:
                    await self._retire_session(entry.session_id, "stale provisional session without tmux pane")
                elif not session_file and entry.started_at and time.time() - entry.started_at > UNKNOWN_PANE_STALE_SECONDS:
                    await self._retire_session(entry.session_id, "stale provisional session without session file")
                continue

            command = pane_current_command(entry.tmux_pane)
            if command and command.lower() in CODEX_ACTIVE_COMMANDS:
                if session_file:
                    await self._maybe_notify_stall(entry, session_file)
                continue

            reason = "tmux pane missing" if not command else f"pane command is {command}"
            await self._retire_session(entry.session_id, reason)

    async def _maybe_notify_stall(self, entry, session_file: Path) -> None:
        """Post one quiet state line when a running turn stops making progress.

        "Did the agent stop?" cannot be answered from turn-complete events —
        those only ever arrive when it did not. The pairing that does answer it
        is a turn known to be in flight plus a rollout that is no longer
        growing, which is exactly the state the operator sat in for four and a
        half hours with no way to see it.

        Deliberately an m.notice: it is a state indicator to find when someone
        looks at the room, not a push. Mirroring the terminal turn error is what
        buzzes the phone.
        """
        if entry.session_id not in self._active_turns:
            return

        try:
            mtime = session_file.stat().st_mtime
        except OSError:
            return

        idle = time.time() - mtime
        if idle < STALL_NOTICE_SECONDS:
            return

        # Re-arm only when the rollout actually moves again, so a session that
        # stays quiet gets one line rather than one every cleanup pass.
        if self._stall_notified.get(entry.session_id) == mtime:
            return
        self._stall_notified[entry.session_id] = mtime

        minutes = int(idle // 60)
        await self.bridge.send_messages(
            entry.session_id,
            [{
                "role": "tool",
                "text": f"⏳ No activity for {minutes} min — turn still marked running.",
            }],
        )
        logger.info(
            "Posted stall notice for %s after %d min of rollout silence",
            entry.session_id[:8], minutes,
        )

    # --- Inbound: Matrix → tmux ---

    async def _matrix_poll_loop(self) -> None:
        """Long-poll Matrix /sync for inbound messages."""
        logger.info("Starting Matrix poll loop for inbound messages")

        # Initial sync to skip old messages
        data = await self.poll_client.sync(timeout=10000)
        self.next_batch = data.get("next_batch")

        while self.running:
            try:
                data = await self.poll_client.sync(
                    since=self.next_batch, timeout=30000,
                )
                self.next_batch = data.get("next_batch")
                await self._process_sync(data)
            except asyncio.TimeoutError:
                # Sync exceeded its hard timeout — connection is likely
                # half-open. Drop the aiohttp session so the next sync
                # opens a fresh TCP connection.
                logger.warning("Matrix sync timed out, reconnecting")
                await self.poll_client.reconnect()
            except Exception as e:
                logger.error(f"Matrix sync error: {type(e).__name__}: {e}")
                await asyncio.sleep(5)

    async def _process_sync(self, data: dict) -> None:
        """Route inbound Matrix messages to the correct tmux pane."""
        rooms = data.get("rooms", {}).get("join", {})

        for room_id, room_data in rooms.items():
            timeline = room_data.get("timeline", {})
            for event in timeline.get("events", []):
                await self._handle_inbound(room_id, event)

    async def _handle_inbound(self, room_id: str, event: dict) -> None:
        """Handle a single inbound Matrix event."""
        if event.get("type") != "m.room.message":
            return

        if event.get("sender") == self.config.user_id:
            return

        content = event.get("content", {})
        msgtype = content.get("msgtype")

        if content.get("cc.catchup"):
            return

        # Only text is handled. Voice arrives as ordinary @admin text via the
        # server-side voicehub STT appservice, so m.audio is ignored here.
        if msgtype == "m.text":
            await self._on_inbound_text(room_id, content.get("body", ""))

    async def _on_inbound_text(self, room_id: str, text: str) -> None:
        """Route a text message from Matrix to the Codex tmux pane."""
        entry = self.session_map.get_by_room(room_id)
        if not entry:
            return

        logger.info(f"Routing to Codex session {entry.session_id[:8]}: {text[:50]}...")
        success = await send_keys(entry.tmux_pane, text)

        if success:
            await self.poll_client.room_typing(
                room_id, self.config.user_id, typing=True, timeout=120000,
            )


def run_daemon():
    """Entry point for the Codex Matrix daemon."""
    from filelock import FileLock, Timeout

    config = load_config()
    if not config:
        print("No config found. Run 'ccmatrix setup' first.", file=sys.stderr)
        sys.exit(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    lock = FileLock(str(STATE_DIR / "codex-daemon.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("Codex daemon is already running.", file=sys.stderr)
        sys.exit(0)

    try:
        daemon_identity = write_daemon_identity(STATE_DIR)
    except RuntimeError as error:
        lock.release()
        print(f"Unable to record Codex daemon identity: {error}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(STATE_DIR / "codex-daemon.log")],
    )

    daemon = CodexDaemon(config)

    def shutdown(signum, frame):
        daemon.request_shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        asyncio.run(daemon.start())
    finally:
        remove_stale_or_owned_pid_record(STATE_DIR, daemon_identity)
        lock.release()


if __name__ == "__main__":
    run_daemon()
