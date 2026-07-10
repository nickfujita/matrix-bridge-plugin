import json
import tempfile
import unittest
from pathlib import Path

from antigravity_matrix.transcript import (
    extract_latest_assistant,
    extract_messages,
    extract_messages_after_count,
)


def _line(**kwargs) -> str:
    base = {
        "step_index": kwargs.pop("step_index", 0),
        "status": kwargs.pop("status", "DONE"),
        "created_at": "2026-06-19T00:00:00Z",
    }
    base.update(kwargs)
    return json.dumps(base)


class AntigravityTranscriptTests(unittest.TestCase):
    def test_extracts_user_tools_and_final_assistant_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript_full.jsonl"
            path.write_text(
                "\n".join(
                    [
                        _line(
                            step_index=0,
                            source="USER_EXPLICIT",
                            type="USER_INPUT",
                            content="<USER_REQUEST>\nBuild the thing\n</USER_REQUEST>",
                        ),
                        _line(
                            step_index=1,
                            source="MODEL",
                            type="PLANNER_RESPONSE",
                            content="I will inspect the repo.",
                            tool_calls=[{"name": "list_dir"}],
                        ),
                        _line(
                            step_index=2,
                            source="MODEL",
                            type="LIST_DIRECTORY",
                            content='Created At: now\n{"name":"README.md"}',
                        ),
                        _line(
                            step_index=3,
                            source="SYSTEM",
                            type="CHECKPOINT",
                            content="internal checkpoint",
                        ),
                        _line(
                            step_index=4,
                            source="MODEL",
                            type="PLANNER_RESPONSE",
                            content="Done. The thing is built.",
                        ),
                    ]
                )
                + "\n"
            )

            self.assertEqual(
                extract_messages(path),
                [
                    {"role": "user", "text": "Build the thing"},
                    {"role": "tool", "text": "● list directory"},
                    {"role": "assistant", "text": "Done. The thing is built."},
                ],
            )
            self.assertEqual(extract_latest_assistant(path), "Done. The thing is built.")

    def test_message_count_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript_full.jsonl"
            path.write_text(
                "\n".join(
                    [
                        _line(source="USER_EXPLICIT", type="USER_INPUT", content="<USER_REQUEST>one</USER_REQUEST>"),
                        _line(source="MODEL", type="PLANNER_RESPONSE", content="two"),
                        _line(source="MODEL", type="PLANNER_RESPONSE", content="three"),
                    ]
                )
                + "\n"
            )

            messages, new_count = extract_messages_after_count(path, 2)
            self.assertEqual(messages, [{"role": "assistant", "text": "three"}])
            self.assertEqual(new_count, 3)

    def test_hidden_automation_prompt_is_not_mirrored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transcript_full.jsonl"
            path.write_text(
                _line(
                    source="USER_EXPLICIT",
                    type="USER_INPUT",
                    content="<USER_REQUEST>PAPER_VOICE_DAILY_RUN_ID=abc</USER_REQUEST>",
                )
                + "\n"
                + _line(source="MODEL", type="PLANNER_RESPONSE", content="daily brief")
                + "\n"
            )

            self.assertEqual(extract_messages(path), [{"role": "assistant", "text": "daily brief"}])


if __name__ == "__main__":
    unittest.main()
