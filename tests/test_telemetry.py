import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telemetry import TokenTracker


class TokenTrackerCacheTests(unittest.TestCase):
    def test_vault_snapshot_uses_cache_within_ttl(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            (vault_path / "note.md").write_text("# Cached\nhello\n", encoding="utf-8")

            tracker = TokenTracker(
                enabled=True,
                log_path=vault_path / "tool_usage.jsonl",
                tokenizer_name="simple",
                hash_filepaths=True,
                include_vault_stats=True,
                vault_stats_cache_ttl_seconds=300,
                vault_path=vault_path,
                ignored_folders=[],
            )

            walk_calls = {"count": 0}
            real_walk = __import__("os").walk

            def counting_walk(*args, **kwargs):
                walk_calls["count"] += 1
                return real_walk(*args, **kwargs)

            with patch("telemetry.os.walk", new=counting_walk):
                first_snapshot = tracker._vault_snapshot()
                second_snapshot = tracker._vault_snapshot()

            self.assertEqual(first_snapshot, second_snapshot)
            self.assertEqual(first_snapshot["vault_note_count"], 1)
            self.assertEqual(walk_calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
