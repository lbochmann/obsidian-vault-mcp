import unittest
import json
import tempfile
from pathlib import Path

from telemetry import TokenTracker


class TokenTrackerTests(unittest.TestCase):
    def test_log_call_and_summary_capture_token_usage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            log_path = base_path / "tool_usage.jsonl"

            tracker = TokenTracker(
                enabled=True,
                log_path=log_path,
                tokenizer_name="simple",
                hash_filepaths=True,
            )

            tracker.log_call(
                kind="tool",
                name="read_note_section",
                args={"filepath": "foo.md"},
                result="short result",
                duration_ms=12.5,
                baseline_result_tokens=25,
            )

            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["name"], "read_note_section")
            self.assertEqual(record["status"], "ok")
            self.assertIn("saved_result_tokens", record)
            self.assertNotIn("vault_note_count", record)
            self.assertNotIn("vault_total_md_bytes", record)

            summary = tracker.summarize_records()
            self.assertEqual(summary["record_count"], 1)
            self.assertEqual(len(summary["by_name"]), 1)
            self.assertEqual(summary["by_name"][0]["name"], "read_note_section")
            self.assertGreaterEqual(summary["totals"]["saved_result_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
