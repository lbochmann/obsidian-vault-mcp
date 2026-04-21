import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def load_server_module():
    if "mcp.server.fastmcp" not in sys.modules:
        mcp_module = types.ModuleType("mcp")
        mcp_server_module = types.ModuleType("mcp.server")
        fastmcp_module = types.ModuleType("mcp.server.fastmcp")

        class FastMCP:
            def __init__(self, *_args, **_kwargs):
                pass

            def tool(self):
                return lambda fn: fn

            def prompt(self):
                return lambda fn: fn

            def run(self):
                return None

        fastmcp_module.FastMCP = FastMCP
        mcp_module.server = mcp_server_module
        mcp_server_module.fastmcp = fastmcp_module

        sys.modules["mcp"] = mcp_module
        sys.modules["mcp.server"] = mcp_server_module
        sys.modules["mcp.server.fastmcp"] = fastmcp_module

    if "server" in sys.modules:
        return importlib.reload(sys.modules["server"])
    return importlib.import_module("server")


server = load_server_module()


class FakeTracker:
    def __init__(self):
        self.calls = []

    def log_call(self, **kwargs):
        self.calls.append(kwargs)

    def count_tokens(self, text):
        return len(text)

    def path_ref(self, path_value):
        return path_value


class SearchAndTelemetryTests(unittest.TestCase):
    def test_search_vault_reports_scan_errors_without_losing_matches(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            (vault_path / "good.md").write_text("# Title\nneedle here\n", encoding="utf-8")
            (vault_path / "second.md").write_text("# Other\nanother needle\n", encoding="utf-8")
            broken_path = vault_path / "broken.md"
            broken_path.write_text("will fail to read", encoding="utf-8")

            tracker = FakeTracker()
            real_open = open

            def guarded_open(file, *args, **kwargs):
                if Path(file) == broken_path:
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated decode failure")
                return real_open(file, *args, **kwargs)

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path), \
                 patch.object(server, "IGNORED_FOLDERS", []), \
                 patch("builtins.open", new=guarded_open):
                response = json.loads(server.search_vault("needle"))

            self.assertTrue(response["ok"])
            self.assertEqual(response["matched_file_count"], 2)
            self.assertEqual(response["scan_error_count"], 1)
            self.assertEqual(response["scan_errors"][0]["file"], "broken.md")
            self.assertGreaterEqual(response["result_count"], 2)

    def test_read_note_invalid_path_is_logged_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tracker = FakeTracker()

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", Path(tmp_dir)):
                response = server.read_note("does-not-exist")

            self.assertIn("Error: File 'does-not-exist.md' not found.", response)
            self.assertEqual(tracker.calls[-1]["status"], "error")

    def test_find_stale_notes_uses_frontmatter_updated_date(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            tracker = FakeTracker()
            note_path = vault_path / "dated-note.md"
            note_path.write_text(
                "---\n"
                "title: Example\n"
                "updated: 2020-01-01\n"
                "---\n\n"
                "# Example\n",
                encoding="utf-8",
            )

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path), \
                 patch.object(server, "IGNORED_FOLDERS", []):
                response = server.find_stale_notes(days_old=30)

            self.assertIn("dated-note.md", response)
            self.assertIn("2020-01-01", response)


class PresidioLanguageTests(unittest.TestCase):
    def test_apply_deep_masking_uses_configured_language(self):
        analyzer_calls = []

        class FakeAnalyzer:
            def analyze(self, text, language):
                analyzer_calls.append({"text": text, "language": language})
                return [{"entity_type": "PERSON"}]

        class FakeAnonymizer:
            def anonymize(self, text, analyzer_results):
                return SimpleNamespace(text="MASKED")

        with patch.object(server, "privacy_rules", []), \
             patch.object(server, "NLP_LANGUAGE", "en"), \
             patch.object(server, "get_presidio_engines", return_value=(FakeAnalyzer(), FakeAnonymizer())):
            result = server.apply_deep_masking("John Doe", masking_mode="required")

        self.assertEqual(result, "MASKED")
        self.assertEqual(analyzer_calls[0]["language"], "en")


if __name__ == "__main__":
    unittest.main()
