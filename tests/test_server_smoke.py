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

    def test_find_unlinked_mentions_ignores_frontmatter_code_and_existing_wikilinks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            tracker = FakeTracker()
            (vault_path / "Alpha Project.md").write_text("# Alpha Project\n", encoding="utf-8")
            (vault_path / "Linked Thing.md").write_text("# Linked Thing\n", encoding="utf-8")
            (vault_path / "Journal.md").write_text(
                "---\n"
                "title: Alpha Project\n"
                "---\n\n"
                "We should connect Alpha Project to the roadmap.\n"
                "This one is already linked: [[Linked Thing]].\n"
                "`Alpha Project` should stay ignored in inline code.\n"
                "```\n"
                "Alpha Project inside a code fence.\n"
                "```\n",
                encoding="utf-8",
            )

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path), \
                 patch.object(server, "IGNORED_FOLDERS", []):
                response = json.loads(server.find_unlinked_mentions())

            self.assertTrue(response["ok"])
            self.assertEqual(response["matched_file_count"], 1)
            self.assertEqual(response["total_result_count"], 1)
            self.assertEqual(response["results"][0]["file"], "Journal.md")
            self.assertEqual(response["results"][0]["note_title"], "Alpha Project")
            self.assertEqual(response["results"][0]["target_paths"], ["Alpha Project.md"])

    def test_search_vault_supports_substring_filepath_filter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            tracker = FakeTracker()
            nested_dir = vault_path / "Projects" / "Example_Area"
            nested_dir.mkdir(parents=True)
            (nested_dir / "02 Example.md").write_text("# Example\nneedle\n", encoding="utf-8")

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path), \
                 patch.object(server, "IGNORED_FOLDERS", []):
                response = json.loads(
                    server.search_vault(
                        "needle",
                        filepath_filter="Example_Area/02",
                        filepath_filter_mode="substring",
                    )
                )

            self.assertTrue(response["ok"])
            self.assertEqual(response["matched_file_count"], 1)
            self.assertEqual(response["results"][0]["file"], "Projects/Example_Area/02 Example.md")


class NoteEditingToolTests(unittest.TestCase):
    def test_append_insert_replace_and_frontmatter_update_are_surgical(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            tracker = FakeTracker()
            note_path = vault_path / "Note.md"
            note_path.write_text(
                "---\n"
                "title: Note\n"
                "updated: 2026-05-01\n"
                "---\n\n"
                "# Note\n\n"
                "Intro.\n\n"
                "## Sources\n"
                "- A\n\n"
                "## Changelog / History\n"
                "- Old\n",
                encoding="utf-8",
            )

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path):
                insert_response = json.loads(server.insert_after_heading("Note", "## Sources", "- B"))
                replace_response = json.loads(server.replace_section("Note", "## Changelog / History", "- New"))
                frontmatter_response = json.loads(
                    server.update_frontmatter("Note", {"updated": "2026-05-08", "status": "complete"})
                )
                append_response = json.loads(server.append_to_note("Note", "## Tail\nDone."))

            updated = note_path.read_text(encoding="utf-8")
            self.assertTrue(insert_response["ok"])
            self.assertTrue(replace_response["ok"])
            self.assertTrue(frontmatter_response["ok"])
            self.assertTrue(append_response["ok"])
            self.assertIn("updated: 2026-05-08\n", updated)
            self.assertIn("status: complete\n", updated)
            self.assertIn("## Sources\n- A\n\n- B\n\n## Changelog / History", updated)
            self.assertIn("## Changelog / History\n\n- New\n\n## Tail\nDone.\n", updated)
            self.assertIn("Intro.", updated)

    def test_update_frontmatter_creates_block_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            tracker = FakeTracker()
            note_path = vault_path / "Plain.md"
            note_path.write_text("# Plain\n", encoding="utf-8")

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path):
                response = json.loads(server.update_frontmatter("Plain", {"updated": "2026-05-08"}))

            self.assertTrue(response["ok"])
            self.assertTrue(response["frontmatter_created"])
            self.assertEqual(note_path.read_text(encoding="utf-8"), "---\nupdated: 2026-05-08\n---\n\n# Plain\n")

    def test_write_note_create_only_refuses_overwrite_and_append_mode_appends(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            tracker = FakeTracker()
            note_path = vault_path / "Existing.md"
            note_path.write_text("# Existing\n", encoding="utf-8")

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path):
                blocked = server.write_note("Existing", "# Replacement\n", mode="create_only")
                appended = server.write_note("Existing", "## Added\n", mode="append")

            self.assertIn("already exists", blocked)
            self.assertIn("mode: append", appended)
            self.assertEqual(note_path.read_text(encoding="utf-8"), "# Existing\n\n## Added\n")

    def test_find_backlinks_and_move_note_update_wikilinks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            vault_path = Path(tmp_dir)
            tracker = FakeTracker()
            source_dir = vault_path / "Projects"
            target_dir = vault_path / "Archive"
            source_dir.mkdir()
            target_dir.mkdir()
            (source_dir / "Alpha.md").write_text("# Alpha\n", encoding="utf-8")
            (vault_path / "Journal.md").write_text(
                "Links: [[Alpha]], [[Projects/Alpha#Details|details]], and Alpha as plain text.\n",
                encoding="utf-8",
            )

            with patch.object(server, "TRACKER", tracker), \
                 patch.object(server, "VAULT_PATH", vault_path), \
                 patch.object(server, "IGNORED_FOLDERS", []):
                backlinks = json.loads(server.find_backlinks("Projects/Alpha.md"))
                moved = json.loads(server.move_note("Projects/Alpha.md", "Archive/Beta.md"))

            self.assertEqual(backlinks["result_count"], 2)
            self.assertTrue(moved["ok"])
            self.assertFalse((source_dir / "Alpha.md").exists())
            self.assertTrue((target_dir / "Beta.md").exists())
            journal = (vault_path / "Journal.md").read_text(encoding="utf-8")
            self.assertIn("[[Beta]]", journal)
            self.assertIn("[[Archive/Beta#Details|details]]", journal)
            self.assertIn("Alpha as plain text", journal)


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
