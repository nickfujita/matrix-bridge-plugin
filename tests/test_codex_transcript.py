import json
import tempfile
import unittest
from pathlib import Path

from codex_matrix.transcript import (
    extract_latest_assistant_after_last_hidden_marker,
    extract_messages,
    extract_messages_from_offset,
    has_hidden_user_marker,
    is_unmirrored_session,
    is_unmirrored_session_meta,
)


def _user_line(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
    )


def _assistant_line(text: str) -> str:
    return json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        }
    )


class CodexTranscriptTests(unittest.TestCase):
    def test_detects_subagent_sessions_as_unmirrored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-07-06T00-00-00-019f364b-1a77-7e22-9847-6598b18c74eb.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "019f364b-1a77-7e22-9847-6598b18c74eb",
                            "parent_thread_id": "019f35ce-6580-7392-aee1-1d2e08ec79bd",
                            "thread_source": "subagent",
                            "source": {"subagent": {"thread_spawn": {"depth": 1}}},
                            "cwd": "/tmp/repo",
                        },
                    }
                )
                + "\n"
            )

            self.assertTrue(is_unmirrored_session(path))
            self.assertTrue(is_unmirrored_session_meta({"thread_source": "subagent"}))
            self.assertFalse(is_unmirrored_session_meta({"thread_source": "user"}))

    def test_hides_paper_voice_daily_automation_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-05-25T00-00-00-019e5e48-b052-7c92-bd72-773558125f1e.jsonl"
            path.write_text(
                "\n".join(
                    [
                        _user_line("Paper Voice proactive daily check-in.\n\nPAPER_VOICE_DAILY_RUN_ID=abc"),
                        _user_line("Please quiz me on the latest chapter."),
                    ]
                )
                + "\n"
            )

            self.assertEqual(
                extract_messages(path),
                [{"role": "user", "text": "Please quiz me on the latest chapter."}],
            )

            messages, _ = extract_messages_from_offset(path, 0)
            self.assertEqual(
                messages,
                [{"role": "user", "text": "Please quiz me on the latest chapter."}],
            )
            self.assertTrue(has_hidden_user_marker(path))

    def test_extracts_latest_assistant_only_after_last_hidden_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-05-25T00-00-00-019e5e48-b052-7c92-bd72-773558125f1e.jsonl"
            path.write_text(
                "\n".join(
                    [
                        _assistant_line("old brief"),
                        _user_line("Paper Voice proactive daily check-in.\n\nPAPER_VOICE_DAILY_RUN_ID=abc"),
                        _assistant_line("first draft"),
                        _assistant_line("final brief"),
                    ]
                )
                + "\n"
            )

            self.assertEqual(
                extract_latest_assistant_after_last_hidden_marker(path),
                "final brief",
            )

    def test_hidden_marker_without_assistant_does_not_replay_old_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-05-25T00-00-00-019e5e48-b052-7c92-bd72-773558125f1e.jsonl"
            path.write_text(
                "\n".join(
                    [
                        _assistant_line("old brief"),
                        _user_line("Paper Voice proactive daily check-in.\n\nPAPER_VOICE_DAILY_RUN_ID=abc"),
                    ]
                )
                + "\n"
            )

            self.assertIsNone(extract_latest_assistant_after_last_hidden_marker(path))


if __name__ == "__main__":
    unittest.main()
