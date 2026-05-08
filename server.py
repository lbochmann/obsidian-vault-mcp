import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from obsidian_mcp import privacy
from obsidian_mcp.config import load_runtime_config
from obsidian_mcp.markdown import (
    append_markdown_block,
    extract_frontmatter,
    find_section_bounds,
    join_markdown_parts,
    normalize_search_text,
    parse_heading_query,
    parse_markdown_heading,
    split_frontmatter_block,
    update_frontmatter_content,
)
from obsidian_mcp.tool_runtime import (
    finalize_tracked_call as finalize_tracked_call_with_tracker,
    serialize_tool_result,
    tracked_error as tracked_error_with_tracker,
)
from obsidian_mcp.vault import (
    filepath_matches_filter,
    is_safe_path as vault_is_safe_path,
    iter_markdown_files,
    normalize_filepath_filter,
    normalize_filepath_filter_mode,
    read_text_file,
    resolve_markdown_target as vault_resolve_markdown_target,
    resolve_vault_target as vault_resolve_vault_target,
    validate_markdown_write_target as vault_validate_markdown_write_target,
    write_text_file,
)
from obsidian_mcp.wikilinks import (
    FENCE_TOGGLE_PATTERN,
    WIKILINK_CAPTURE_PATTERN,
    build_wikilink_replacement,
    collect_note_title_candidates,
    find_wikilink_backlinks,
    normalize_wikilink_target,
    resolve_backlink_aliases,
    sanitize_line_for_unlinked_mentions,
    wikilink_aliases_for_path,
)
from telemetry import TokenTracker


VALID_WRITE_MODES = {"overwrite", "create_only", "append"}
VALID_INSERT_PLACEMENTS = {"end_of_section", "after_heading"}

BASE_DIR = Path(__file__).parent
runtime_config = load_runtime_config(BASE_DIR)

config = runtime_config["config"]
CONFIG_PATH = runtime_config["config_path"]
VAULT_PATH = runtime_config["vault_path"]
IGNORED_FOLDERS = runtime_config["ignored_folders"]
privacy_data = runtime_config["privacy_data"]
PRIVACY_PATH = runtime_config["privacy_path"]
privacy_rules = runtime_config["privacy_rules"]
NLP_LANGUAGE = runtime_config["nlp_language"]
PRESIDIO_MODEL = runtime_config["presidio_model"]

privacy.configure_privacy(
    nlp_language=NLP_LANGUAGE,
    presidio_model=PRESIDIO_MODEL,
    rules=privacy_rules,
)

PRESIDIO_AVAILABLE = privacy.PRESIDIO_AVAILABLE
ANALYZER_ENGINE = None
ANONYMIZER_ENGINE = None

mcp = FastMCP("obsidian-vault-mcp")
TRACKER = TokenTracker.from_config(
    config=config,
    base_dir=BASE_DIR,
)


def finalize_tracked_call(
    kind: str,
    name: str,
    started_at: float,
    args: dict,
    result,
    *,
    meta: dict | None = None,
    baseline_result_tokens: int | None = None,
    status: str = "ok",
) -> str:
    return finalize_tracked_call_with_tracker(
        TRACKER,
        kind,
        name,
        started_at,
        args,
        result,
        meta=meta,
        baseline_result_tokens=baseline_result_tokens,
        status=status,
    )


def tracked_error(
    kind: str,
    name: str,
    started_at: float,
    args: dict,
    result,
    *,
    meta: dict | None = None,
    baseline_result_tokens: int | None = None,
) -> str:
    return tracked_error_with_tracker(
        TRACKER,
        kind,
        name,
        started_at,
        args,
        result,
        meta=meta,
        baseline_result_tokens=baseline_result_tokens,
    )


def is_safe_path(requested_path: str) -> bool:
    return vault_is_safe_path(VAULT_PATH, requested_path)


def resolve_vault_target(filepath: str) -> Path:
    return vault_resolve_vault_target(VAULT_PATH, filepath)


def resolve_markdown_target(filepath: str) -> tuple[str, Path]:
    return vault_resolve_markdown_target(VAULT_PATH, filepath)


def validate_markdown_write_target(filepath: str, target_file: Path) -> str | None:
    return vault_validate_markdown_write_target(VAULT_PATH, filepath, target_file)


def get_presidio_engines():
    return privacy.get_presidio_engines()


def apply_masking(text: str) -> str:
    """Applies Regex filters to mask personally identifiable information before passing it to the LLM."""
    if not privacy_rules:
        return text

    masked_text = text
    for rule in privacy_rules:
        pattern = rule.get("pattern")
        replacement = rule.get("replacement")
        if pattern and replacement:
            masked_text = re.sub(pattern, replacement, masked_text)

    return masked_text


def get_masking_mode(text: str) -> str:
    return privacy.get_masking_mode(text)


def apply_deep_masking(text: str, masking_mode: str = "balanced") -> str:
    """Applies masking with per-note policy control for technical versus sensitive content."""
    if masking_mode == "clear":
        return text

    masked_text = apply_masking(text)

    if masking_mode == "required":
        protected_text = masked_text
        protected_segments: dict[str, str] = {}
    else:
        protected_text, protected_segments = privacy.protect_special_segments(masked_text)

    analyzer, anonymizer = get_presidio_engines()
    if analyzer and anonymizer:
        try:
            results = analyzer.analyze(text=protected_text, language=NLP_LANGUAGE)
            if results:
                anonymized_result = anonymizer.anonymize(text=protected_text, analyzer_results=results)
                protected_text = anonymized_result.text
        except Exception as e:
            print(f"Warning: Presidio deep masking failed: {e}")

    return privacy.restore_special_segments(protected_text, protected_segments)


def build_search_snippet(lines: list[str], match_index: int, context_lines: int) -> tuple[str, int, int]:
    start_line = max(match_index - max(context_lines, 0), 0)
    end_line = min(match_index + max(context_lines, 0) + 1, len(lines))
    snippet_lines = []

    for idx in range(start_line, end_line):
        prefix = ">" if idx == match_index else " "
        snippet_lines.append(f"{prefix} {lines[idx].strip()}")

    snippet_text = "\n".join(snippet_lines)
    return apply_masking(snippet_text), start_line + 1, end_line


@mcp.tool()
def list_notes(directory: str = "") -> str:
    """Lists all Markdown files in the Vault or a specific subfolder."""
    started_at = time.perf_counter()
    target_dir = (VAULT_PATH / directory).resolve()
    meta = {"directory_ref": TRACKER.path_ref(directory)}

    if not is_safe_path(directory) or not target_dir.exists():
        return finalize_tracked_call(
            "tool",
            "list_notes",
            started_at,
            {"directory": directory},
            f"Error: Path '{directory}' does not exist or is invalid.",
            meta=meta,
            status="error",
        )

    files = []
    for root, dirs, filenames in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]

        for name in filenames:
            if name.endswith(".md"):
                rel_path = os.path.relpath(os.path.join(root, name), VAULT_PATH)
                files.append(rel_path)

    if not files:
        return finalize_tracked_call(
            "tool",
            "list_notes",
            started_at,
            {"directory": directory},
            "No Markdown files found.",
            meta=meta | {"note_count": 0},
        )

    result = "Found notes:\n" + "\n".join(f"- {f}" for f in sorted(files))
    return finalize_tracked_call(
        "tool",
        "list_notes",
        started_at,
        {"directory": directory},
        result,
        meta=meta | {"note_count": len(files)},
    )


@mcp.tool()
def get_vault_structure(max_depth: int = 2) -> str:
    """Returns the folder structure (directories only) of the Vault as a tree."""
    started_at = time.perf_counter()

    def generate_tree(dir_path: Path, current_depth: int) -> list:
        if current_depth >= max_depth:
            return []

        tree_lines = []
        try:
            entries = sorted(os.listdir(dir_path))
        except PermissionError:
            return []

        for name in entries:
            full_path = dir_path / name
            if full_path.is_dir() and name not in IGNORED_FOLDERS and not name.startswith('.'):
                indent = "  " * current_depth
                tree_lines.append(f"{indent}📂 {name}/")
                tree_lines.extend(generate_tree(full_path, current_depth + 1))

        return tree_lines

    tree = generate_tree(VAULT_PATH, 0)
    if not tree:
        return finalize_tracked_call(
            "tool",
            "get_vault_structure",
            started_at,
            {"max_depth": max_depth},
            "No directories found or depth limit reached.",
            meta={"max_depth": max_depth, "directory_count": 0},
        )

    result = f"Vault Folder Structure (Max Depth {max_depth}):\n" + "\n".join(tree)
    return finalize_tracked_call(
        "tool",
        "get_vault_structure",
        started_at,
        {"max_depth": max_depth},
        result,
        meta={"max_depth": max_depth, "directory_count": len(tree)},
    )


@mcp.tool()
def read_note(filepath: str) -> str:
    """Reads the content of a specific Markdown file."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}

    if not is_safe_path(filepath) or not target_file.exists():
        return tracked_error(
            "tool",
            "read_note",
            started_at,
            {"filepath": filepath},
            f"Error: File '{filepath}' not found.",
            meta=meta,
        )

    content = read_text_file(target_file)
    masking_mode = get_masking_mode(content)
    masked_content = apply_deep_masking(content, masking_mode=masking_mode)

    analyzer, _ = get_presidio_engines()
    if not analyzer and masking_mode != "clear":
        warning_block = (
            "> [!WARNING] SYSTEM NOTE TO LLM\n"
            f"> The Deep PII NLP Filter (Presidio, configured language: {NLP_LANGUAGE}) is currently unavailable or failed to load. "
            "> Only basic Regex domain masking was applied to this file. "
            "> YOU MUST explicitly warn the user about this in your response so they are aware.\n\n"
        )
        masked_content = warning_block + masked_content

    return finalize_tracked_call(
        "tool",
        "read_note",
        started_at,
        {"filepath": filepath},
        masked_content,
        meta=meta | {
            "source_chars": len(content),
            "result_chars": len(masked_content),
            "presidio_available": bool(analyzer),
            "masking_mode": masking_mode,
            "nlp_language": NLP_LANGUAGE,
        },
    )


@mcp.tool()
def search_vault(
    query: str,
    include_filenames: bool = True,
    context_lines: int = 1,
    filepath_filter: str = "",
    filepath_filter_mode: str = "prefix",
) -> str:
    """Searches note contents and filenames and returns structured match data."""
    started_at = time.perf_counter()
    query_raw = query
    query = query.lower()
    filepath_filter_normalized = normalize_filepath_filter(filepath_filter)
    normalized_filter_mode = normalize_filepath_filter_mode(filepath_filter_mode)
    results = []
    matched_file_count = 0
    baseline_result_tokens = 0
    scan_errors = []
    scan_error_count = 0
    max_scan_errors = 5

    for rel_path, file_path in iter_markdown_files(VAULT_PATH, IGNORED_FOLDERS):
        try:
            content = read_text_file(file_path)
            lines = content.splitlines()
            rel_path_lower = rel_path.lower()

            if not filepath_matches_filter(rel_path, filepath_filter_normalized, normalized_filter_mode):
                continue

            filename_matched = include_filenames and query in rel_path_lower
            content_matches = []

            for i, line in enumerate(lines):
                if query in line.lower():
                    snippet_masked, start_line, end_line = build_search_snippet(lines, i, context_lines)
                    content_matches.append(
                        {
                            "file": rel_path,
                            "match_type": "both" if filename_matched else "content",
                            "line": i + 1,
                            "line_range": [start_line, end_line],
                            "snippet_markdown": snippet_masked,
                        }
                    )

            if filename_matched or content_matches:
                matched_file_count += 1
                baseline_result_tokens += TRACKER.count_tokens(content)

            if content_matches:
                results.extend(content_matches)
            elif filename_matched:
                results.append(
                    {
                        "file": rel_path,
                        "match_type": "filename",
                        "line": None,
                        "line_range": None,
                        "snippet_markdown": None,
                    }
                )
        except Exception as exc:
            scan_error_count += 1
            if len(scan_errors) < max_scan_errors:
                scan_errors.append(
                    {
                        "file": rel_path,
                        "error_type": type(exc).__name__,
                    }
                )

    request_args = {
        "query": query_raw,
        "include_filenames": include_filenames,
        "context_lines": context_lines,
        "filepath_filter": filepath_filter,
        "filepath_filter_mode": filepath_filter_mode,
    }
    base_payload = {
        "ok": True,
        "query": query_raw,
        "filepath_filter": filepath_filter_normalized or None,
        "filepath_filter_mode": normalized_filter_mode,
        "include_filenames": include_filenames,
        "context_lines": context_lines,
        "scan_error_count": scan_error_count,
        "scan_errors_truncated": scan_error_count > len(scan_errors),
        "scan_errors": scan_errors,
    }

    if not results:
        return finalize_tracked_call(
            "tool",
            "search_vault",
            started_at,
            request_args,
            base_payload | {
                "matched_file_count": 0,
                "result_count": 0,
                "total_result_count": 0,
                "results_truncated": False,
                "results": [],
            },
            meta={
                "query_length": len(query),
                "match_count": 0,
                "matched_file_count": 0,
                "scan_error_count": scan_error_count,
                "include_filenames": include_filenames,
                "context_lines": context_lines,
                "filepath_filter": filepath_filter_normalized or None,
                "filepath_filter_mode": normalized_filter_mode,
            },
            baseline_result_tokens=0,
        )

    max_results = 20
    truncated_results = results[:max_results]
    result = base_payload | {
        "matched_file_count": matched_file_count,
        "result_count": len(truncated_results),
        "total_result_count": len(results),
        "results_truncated": len(results) > max_results,
        "results": truncated_results,
    }
    return finalize_tracked_call(
        "tool",
        "search_vault",
        started_at,
        request_args,
        result,
        meta={
            "query_length": len(query),
            "match_count": len(results),
            "matched_file_count": matched_file_count,
            "scan_error_count": scan_error_count,
            "max_results": max_results,
            "include_filenames": include_filenames,
            "context_lines": context_lines,
            "filepath_filter": filepath_filter_normalized or None,
            "filepath_filter_mode": normalized_filter_mode,
            "baseline_strategy": "full_matched_files_raw",
        },
        baseline_result_tokens=baseline_result_tokens,
    )


@mcp.tool()
def get_note_outline(filepath: str) -> str:
    """Returns a semantic outline (table of contents) of a Markdown file by extracting its headers."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}

    if not is_safe_path(filepath) or not target_file.exists():
        return tracked_error(
            "tool",
            "get_note_outline",
            started_at,
            {"filepath": filepath},
            f"Error: File '{filepath}' not found.",
            meta=meta,
        )

    lines = target_file.read_text(encoding="utf-8").splitlines(keepends=True)
    outline = [line.strip() for line in lines if line.startswith("#")]

    if not outline:
        return finalize_tracked_call(
            "tool",
            "get_note_outline",
            started_at,
            {"filepath": filepath},
            "No headers found in this file.",
            meta=meta | {"header_count": 0},
            baseline_result_tokens=TRACKER.count_tokens("".join(lines)),
        )

    result = "Note Outline:\n" + "\n".join(outline)
    return finalize_tracked_call(
        "tool",
        "get_note_outline",
        started_at,
        {"filepath": filepath},
        result,
        meta=meta | {
            "header_count": len(outline),
            "baseline_strategy": "full_note_raw",
        },
        baseline_result_tokens=TRACKER.count_tokens("".join(lines)),
    )


@mcp.tool()
def read_note_section(
    filepath: str,
    heading: str,
    offset_lines: int = 0,
    max_lines: int = 40,
    heading_fuzzy: bool = False,
) -> str:
    """Reads a specific section under a heading and returns structured pagination metadata."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    request_args = {
        "filepath": filepath,
        "heading": heading,
        "offset_lines": offset_lines,
        "max_lines": max_lines,
        "heading_fuzzy": heading_fuzzy,
    }
    meta = {
        "filepath_ref": TRACKER.path_ref(filepath),
        "heading_query_ref": TRACKER.path_ref(normalize_search_text(heading)),
    }

    if not is_safe_path(filepath) or not target_file.exists():
        return tracked_error(
            "tool",
            "read_note_section",
            started_at,
            request_args,
            {"ok": False, "error": f"File '{filepath}' not found."},
            meta=meta,
        )

    lines = target_file.read_text(encoding="utf-8").splitlines(keepends=True)
    file_content = "".join(lines)
    masking_mode = get_masking_mode(file_content)
    requested_level, _ = parse_heading_query(heading)
    bounds = find_section_bounds(lines, heading, heading_fuzzy=heading_fuzzy)

    if not bounds["ok"]:
        return tracked_error(
            "tool",
            "read_note_section",
            started_at,
            request_args,
            {"ok": False, "requested_heading": heading, **bounds},
            meta=meta | {
                "heading_level": requested_level or 0,
                "available_heading_count": bounds.get("available_heading_count", 0),
                "heading_fuzzy": heading_fuzzy,
            },
        )

    section_lines = lines[bounds["start"]:bounds["end"]]
    heading_line = section_lines[0]
    body_lines = section_lines[1:]
    normalized_offset = max(offset_lines, 0)

    if normalized_offset > len(body_lines):
        return tracked_error(
            "tool",
            "read_note_section",
            started_at,
            request_args,
            {
                "ok": False,
                "error": f"offset_lines {offset_lines} exceeds the section length of {len(body_lines)} body lines.",
                "requested_offset": offset_lines,
                "section_total_body_lines": len(body_lines),
            },
            meta=meta | {
                "heading_level": bounds["heading_level"],
                "section_total_body_lines": len(body_lines),
            },
        )

    if max_lines > 0:
        chunk_body_lines = body_lines[normalized_offset: normalized_offset + max_lines]
        next_offset = normalized_offset + len(chunk_body_lines)
        truncated = next_offset < len(body_lines)
    else:
        chunk_body_lines = body_lines[normalized_offset:]
        next_offset = normalized_offset + len(chunk_body_lines)
        truncated = False

    content = "".join([heading_line] + chunk_body_lines)
    masked_content = apply_deep_masking(content, masking_mode=masking_mode)

    analyzer, _ = get_presidio_engines()
    if not analyzer and masking_mode != "clear":
        warning_block = (
            "> [!WARNING] SYSTEM NOTE TO LLM\n"
            f"> The Deep PII NLP Filter (Presidio, configured language: {NLP_LANGUAGE}) is currently unavailable or failed to load. "
            "> Only basic Regex domain masking was applied. "
            "> YOU MUST explicitly warn the user about this in your response.\n\n"
        )
        masked_content = warning_block + masked_content

    returned_start = normalized_offset + 1 if chunk_body_lines else 0
    returned_end = next_offset if chunk_body_lines else normalized_offset
    response_payload = {
        "ok": True,
        "filepath": filepath,
        "requested_heading": heading,
        "resolved_heading": bounds["resolved_heading"],
        "heading_level": bounds["heading_level"],
        "match_strategy": bounds["match_strategy"],
        "match_confidence": round(bounds["match_confidence"], 4),
        "content_markdown": masked_content,
        "masking_mode": masking_mode,
        "presidio_available": bool(analyzer),
        "nlp_language": NLP_LANGUAGE,
        "truncated": truncated,
        "offset_lines": normalized_offset,
        "max_lines": max_lines,
        "returned_body_lines": len(chunk_body_lines),
        "returned_line_range": [returned_start, returned_end] if chunk_body_lines else [],
        "section_total_body_lines": len(body_lines),
        "next_offset": next_offset if truncated else None,
    }

    return finalize_tracked_call(
        "tool",
        "read_note_section",
        started_at,
        request_args,
        response_payload,
        meta=meta | {
            "heading_level": bounds["heading_level"],
            "section_chars": len(content),
            "section_total_body_lines": len(body_lines),
            "offset_lines": normalized_offset,
            "max_lines": max_lines,
            "returned_body_lines": len(chunk_body_lines),
            "truncated": truncated,
            "heading_fuzzy": heading_fuzzy,
            "match_strategy": bounds["match_strategy"],
            "match_confidence": round(bounds["match_confidence"], 4),
            "baseline_strategy": "full_note_raw",
            "presidio_available": bool(analyzer),
            "masking_mode": masking_mode,
            "nlp_language": NLP_LANGUAGE,
        },
        baseline_result_tokens=TRACKER.count_tokens("".join(lines)),
    )


@mcp.tool()
def append_to_note(filepath: str, content: str) -> str:
    """Appends Markdown content to the end of an existing note without rewriting the full file."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    request_args = {"filepath": filepath, "content": content}
    meta = {"filepath_ref": TRACKER.path_ref(filepath), "content_chars": len(content)}

    if not is_safe_path(filepath) or not target_file.exists():
        return tracked_error(
            "tool",
            "append_to_note",
            started_at,
            request_args,
            {"ok": False, "error": f"File '{filepath}' not found or invalid path."},
            meta=meta,
        )

    existing_content = read_text_file(target_file)
    updated_content = append_markdown_block(existing_content, content)
    write_text_file(target_file, updated_content)

    return finalize_tracked_call(
        "tool",
        "append_to_note",
        started_at,
        request_args,
        {
            "ok": True,
            "filepath": filepath,
            "operation": "append",
            "content_chars": len(content),
            "source_chars": len(existing_content),
            "result_chars": len(updated_content),
        },
        meta=meta | {"source_chars": len(existing_content), "result_chars": len(updated_content)},
    )


@mcp.tool()
def insert_after_heading(
    filepath: str,
    heading: str,
    content: str,
    placement: str = "end_of_section",
    heading_fuzzy: bool = False,
) -> str:
    """Inserts Markdown content after a heading or at the end of that heading's section."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    normalized_placement = placement.strip().lower()
    if normalized_placement not in VALID_INSERT_PLACEMENTS:
        normalized_placement = "end_of_section"

    request_args = {
        "filepath": filepath,
        "heading": heading,
        "content": content,
        "placement": placement,
        "heading_fuzzy": heading_fuzzy,
    }
    meta = {
        "filepath_ref": TRACKER.path_ref(filepath),
        "heading_query_ref": TRACKER.path_ref(normalize_search_text(heading)),
        "content_chars": len(content),
    }

    if not is_safe_path(filepath) or not target_file.exists():
        return tracked_error(
            "tool",
            "insert_after_heading",
            started_at,
            request_args,
            {"ok": False, "error": f"File '{filepath}' not found or invalid path."},
            meta=meta,
        )

    existing_content = read_text_file(target_file)
    lines = existing_content.splitlines(keepends=True)
    bounds = find_section_bounds(lines, heading, heading_fuzzy=heading_fuzzy)
    if not bounds["ok"]:
        return tracked_error("tool", "insert_after_heading", started_at, request_args, bounds, meta=meta)

    insert_index = bounds["start"] + 1 if normalized_placement == "after_heading" else bounds["end"]
    updated_content = join_markdown_parts("".join(lines[:insert_index]), content, "".join(lines[insert_index:]))
    write_text_file(target_file, updated_content)

    return finalize_tracked_call(
        "tool",
        "insert_after_heading",
        started_at,
        request_args,
        {
            "ok": True,
            "filepath": filepath,
            "operation": "insert_after_heading",
            "requested_heading": heading,
            "resolved_heading": bounds["resolved_heading"],
            "heading_level": bounds["heading_level"],
            "match_strategy": bounds["match_strategy"],
            "match_confidence": bounds["match_confidence"],
            "placement": normalized_placement,
            "insert_line": insert_index + 1,
            "content_chars": len(content),
        },
        meta=meta | {
            "heading_level": bounds["heading_level"],
            "match_strategy": bounds["match_strategy"],
            "placement": normalized_placement,
        },
    )


@mcp.tool()
def replace_section(filepath: str, heading: str, new_content: str, heading_fuzzy: bool = False) -> str:
    """Replaces one Markdown section while preserving the rest of the note."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    request_args = {
        "filepath": filepath,
        "heading": heading,
        "new_content": new_content,
        "heading_fuzzy": heading_fuzzy,
    }
    meta = {
        "filepath_ref": TRACKER.path_ref(filepath),
        "heading_query_ref": TRACKER.path_ref(normalize_search_text(heading)),
        "content_chars": len(new_content),
    }

    if not is_safe_path(filepath) or not target_file.exists():
        return tracked_error(
            "tool",
            "replace_section",
            started_at,
            request_args,
            {"ok": False, "error": f"File '{filepath}' not found or invalid path."},
            meta=meta,
        )

    existing_content = read_text_file(target_file)
    lines = existing_content.splitlines(keepends=True)
    bounds = find_section_bounds(lines, heading, heading_fuzzy=heading_fuzzy)
    if not bounds["ok"]:
        return tracked_error("tool", "replace_section", started_at, request_args, bounds, meta=meta)

    replacement = new_content.strip("\n")
    replacement_first_line = replacement.splitlines()[0] if replacement else ""
    if not parse_markdown_heading(replacement_first_line):
        replacement = f"{bounds['resolved_heading']}\n\n{replacement}".rstrip("\n")

    updated_content = join_markdown_parts("".join(lines[:bounds["start"]]), replacement, "".join(lines[bounds["end"]:]))
    write_text_file(target_file, updated_content)

    return finalize_tracked_call(
        "tool",
        "replace_section",
        started_at,
        request_args,
        {
            "ok": True,
            "filepath": filepath,
            "operation": "replace_section",
            "requested_heading": heading,
            "resolved_heading": bounds["resolved_heading"],
            "heading_level": bounds["heading_level"],
            "match_strategy": bounds["match_strategy"],
            "match_confidence": bounds["match_confidence"],
            "replaced_line_range": [bounds["start"] + 1, bounds["end"]],
            "content_chars": len(new_content),
        },
        meta=meta | {
            "heading_level": bounds["heading_level"],
            "match_strategy": bounds["match_strategy"],
            "replaced_lines": bounds["end"] - bounds["start"],
        },
    )


@mcp.tool()
def update_frontmatter(filepath: str, updates: dict) -> str:
    """Updates only YAML frontmatter keys, creating a frontmatter block when needed."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    request_args = {"filepath": filepath, "updates": updates}
    meta = {
        "filepath_ref": TRACKER.path_ref(filepath),
        "updated_keys": sorted(str(key) for key in updates.keys()) if isinstance(updates, dict) else [],
    }

    if not isinstance(updates, dict) or not updates:
        return tracked_error(
            "tool",
            "update_frontmatter",
            started_at,
            request_args,
            {"ok": False, "error": "updates must be a non-empty object."},
            meta=meta,
        )

    if not is_safe_path(filepath) or not target_file.exists():
        return tracked_error(
            "tool",
            "update_frontmatter",
            started_at,
            request_args,
            {"ok": False, "error": f"File '{filepath}' not found or invalid path."},
            meta=meta,
        )

    existing_content = read_text_file(target_file)
    try:
        updated_content = update_frontmatter_content(existing_content, updates)
    except ValueError as exc:
        return tracked_error(
            "tool",
            "update_frontmatter",
            started_at,
            request_args,
            {"ok": False, "error": str(exc)},
            meta=meta,
        )

    write_text_file(target_file, updated_content)

    return finalize_tracked_call(
        "tool",
        "update_frontmatter",
        started_at,
        request_args,
        {
            "ok": True,
            "filepath": filepath,
            "operation": "update_frontmatter",
            "updated_keys": sorted(str(key) for key in updates.keys()),
            "frontmatter_created": not bool(split_frontmatter_block(existing_content)[2]),
        },
        meta=meta,
    )


@mcp.tool()
def write_note(filepath: str, content: str, mode: str = "overwrite") -> str:
    """Creates, overwrites, or appends to a Markdown note. Use create_only when overwrites would be unsafe."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    normalized_mode = mode.strip().lower()
    if normalized_mode not in VALID_WRITE_MODES:
        normalized_mode = "overwrite"
    meta = {"filepath_ref": TRACKER.path_ref(filepath), "content_chars": len(content), "mode": normalized_mode}

    validation_error = validate_markdown_write_target(filepath, target_file)
    if validation_error:
        return tracked_error(
            "tool",
            "write_note",
            started_at,
            {"filepath": filepath, "content": content, "mode": mode},
            validation_error,
            meta=meta,
        )

    if normalized_mode == "create_only" and target_file.exists():
        return tracked_error(
            "tool",
            "write_note",
            started_at,
            {"filepath": filepath, "content": content, "mode": mode},
            f"Error: File '{filepath}' already exists. Use mode='overwrite' or a surgical edit tool if you intend to modify it.",
            meta=meta,
        )

    if normalized_mode == "append" and target_file.exists():
        final_content = append_markdown_block(read_text_file(target_file), content)
    else:
        final_content = content

    write_text_file(target_file, final_content)

    return finalize_tracked_call(
        "tool",
        "write_note",
        started_at,
        {"filepath": filepath, "content": content, "mode": mode},
        f"Note successfully written: {filepath} (mode: {normalized_mode})",
        meta=meta,
    )


@mcp.tool()
def find_backlinks(filepath_or_title: str, filepath_filter: str = "", filepath_filter_mode: str = "prefix") -> str:
    """Finds notes that contain real Obsidian-style wikilinks to a target note."""
    started_at = time.perf_counter()
    filepath_filter_normalized = normalize_filepath_filter(filepath_filter)
    normalized_filter_mode = normalize_filepath_filter_mode(filepath_filter_mode)
    target_aliases = resolve_backlink_aliases(VAULT_PATH, filepath_or_title)
    request_args = {
        "filepath_or_title": filepath_or_title,
        "filepath_filter": filepath_filter,
        "filepath_filter_mode": filepath_filter_mode,
    }
    meta = {
        "target_ref": TRACKER.path_ref(filepath_or_title),
        "filepath_filter": filepath_filter_normalized or None,
        "filepath_filter_mode": normalized_filter_mode,
    }

    if not target_aliases:
        return tracked_error(
            "tool",
            "find_backlinks",
            started_at,
            request_args,
            {"ok": False, "error": "filepath_or_title must not be empty."},
            meta=meta,
        )

    backlink_payload = find_wikilink_backlinks(
        vault_path=VAULT_PATH,
        ignored_folders=IGNORED_FOLDERS,
        target_aliases=target_aliases,
        filepath_filter=filepath_filter_normalized,
        filepath_filter_mode=normalized_filter_mode,
        filepath_matches_filter=filepath_matches_filter,
    )
    response_payload = {
        "ok": True,
        "filepath_or_title": filepath_or_title,
        "filepath_filter": filepath_filter_normalized or None,
        "filepath_filter_mode": normalized_filter_mode,
        **backlink_payload,
    }
    return finalize_tracked_call(
        "tool",
        "find_backlinks",
        started_at,
        request_args,
        response_payload,
        meta=meta | {
            "matched_file_count": backlink_payload["matched_file_count"],
            "result_count": backlink_payload["result_count"],
            "scanned_file_count": backlink_payload["scanned_file_count"],
            "scan_error_count": backlink_payload["scan_error_count"],
        },
    )


@mcp.tool()
def move_note(source_filepath: str, target_filepath: str, update_links: bool = True) -> str:
    """Moves or renames a note and optionally updates unambiguous wikilinks to it."""
    started_at = time.perf_counter()
    source_filepath, source_file = resolve_markdown_target(source_filepath)
    target_filepath, target_file = resolve_markdown_target(target_filepath)
    request_args = {
        "source_filepath": source_filepath,
        "target_filepath": target_filepath,
        "update_links": update_links,
    }
    meta = {
        "source_ref": TRACKER.path_ref(source_filepath),
        "target_ref": TRACKER.path_ref(target_filepath),
        "update_links": update_links,
    }

    if not is_safe_path(source_filepath) or not source_file.exists():
        return tracked_error(
            "tool",
            "move_note",
            started_at,
            request_args,
            {"ok": False, "error": f"Source file '{source_filepath}' not found or invalid path."},
            meta=meta,
        )

    validation_error = validate_markdown_write_target(target_filepath, target_file)
    if validation_error:
        return tracked_error("tool", "move_note", started_at, request_args, {"ok": False, "error": validation_error}, meta=meta)

    if target_file.exists():
        return tracked_error(
            "tool",
            "move_note",
            started_at,
            request_args,
            {"ok": False, "error": f"Target file '{target_filepath}' already exists."},
            meta=meta,
        )

    old_aliases = wikilink_aliases_for_path(source_filepath)
    old_stem_alias = normalize_wikilink_target(Path(source_filepath).stem)
    duplicate_title_paths = [
        rel_path for rel_path, _ in iter_markdown_files(VAULT_PATH, IGNORED_FOLDERS)
        if rel_path != source_filepath and normalize_search_text(Path(rel_path).stem) == normalize_search_text(Path(source_filepath).stem)
    ]

    if update_links and duplicate_title_paths:
        for _, file_path in iter_markdown_files(VAULT_PATH, IGNORED_FOLDERS):
            content = read_text_file(file_path)
            for match in WIKILINK_CAPTURE_PATTERN.finditer(content):
                if normalize_wikilink_target(match.group(1)) == old_stem_alias:
                    return tracked_error(
                        "tool",
                        "move_note",
                        started_at,
                        request_args,
                        {
                            "ok": False,
                            "error": "Refusing to update title-only links because another note has the same title.",
                            "ambiguous_title": Path(source_filepath).stem,
                            "duplicate_paths": duplicate_title_paths,
                        },
                        meta=meta | {"ambiguous_duplicate_count": len(duplicate_title_paths)},
                    )

    target_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_file), str(target_file))

    updated_link_files = []
    updated_link_count = 0
    if update_links:
        for rel_path, file_path in iter_markdown_files(VAULT_PATH, IGNORED_FOLDERS):
            try:
                content = read_text_file(file_path)
            except Exception:
                continue

            replacement_count = 0

            def replace_match(match) -> str:
                nonlocal replacement_count
                replacement = build_wikilink_replacement(match, old_aliases, target_filepath)
                if replacement != match.group(0):
                    replacement_count += 1
                return replacement

            updated_content = WIKILINK_CAPTURE_PATTERN.sub(replace_match, content)
            if replacement_count:
                write_text_file(file_path, updated_content)
                updated_link_files.append(rel_path)
                updated_link_count += replacement_count

    return finalize_tracked_call(
        "tool",
        "move_note",
        started_at,
        request_args,
        {
            "ok": True,
            "operation": "move_note",
            "source_filepath": source_filepath,
            "target_filepath": target_filepath,
            "updated_link_count": updated_link_count,
            "updated_link_file_count": len(updated_link_files),
            "updated_link_files": updated_link_files,
        },
        meta=meta | {"updated_link_count": updated_link_count, "updated_link_file_count": len(updated_link_files)},
    )


@mcp.tool()
def archive_note(filepath: str) -> str:
    """Moves a processed note from the Inbox or Clippings folder to the Archive folder to keep the workspace clean."""
    started_at = time.perf_counter()
    filepath, source_file = resolve_markdown_target(filepath)
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}

    if not is_safe_path(filepath) or not source_file.exists():
        return tracked_error(
            "tool",
            "archive_note",
            started_at,
            {"filepath": filepath},
            f"Error: File '{filepath}' not found or invalid path.",
            meta=meta,
        )

    rel_path = os.path.relpath(source_file, VAULT_PATH)
    if not (rel_path.startswith("00_Inbox") or rel_path.startswith("Clippings")):
        return tracked_error(
            "tool",
            "archive_note",
            started_at,
            {"filepath": filepath},
            f"Error: Archiving is only permitted for the '00_Inbox' or 'Clippings' folders to prevent accidental moves. Path was '{rel_path}'.",
            meta=meta | {"source_ref": TRACKER.path_ref(rel_path)},
        )

    archive_dir = (VAULT_PATH / "08_Archive" / "Processed_Clippings").resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)

    target_file = archive_dir / source_file.name
    counter = 1
    while target_file.exists():
        target_file = archive_dir / f"{source_file.stem}_{counter}.md"
        counter += 1

    shutil.move(str(source_file), str(target_file))

    return finalize_tracked_call(
        "tool",
        "archive_note",
        started_at,
        {"filepath": filepath},
        f"Success! Note moved from '{rel_path}' to '{os.path.relpath(target_file, VAULT_PATH)}'.",
        meta=meta | {"source_ref": TRACKER.path_ref(rel_path)},
    )


@mcp.tool()
def find_stale_notes(days_old: int = 90) -> str:
    """Finds notes that haven't been updated in a given number of days to help review stale content."""
    started_at = time.perf_counter()
    stale_files = []
    now = datetime.now()

    for rel_path, file_path in iter_markdown_files(VAULT_PATH, IGNORED_FOLDERS):
        last_updated = None
        try:
            content = read_text_file(file_path)
            updated_value = extract_frontmatter(content).get("updated", "").strip().strip("'\"")
            if updated_value:
                try:
                    last_updated = datetime.strptime(updated_value, "%Y-%m-%d")
                except ValueError:
                    pass
        except Exception:
            pass

        if not last_updated:
            try:
                mtime = os.path.getmtime(file_path)
                last_updated = datetime.fromtimestamp(mtime)
            except OSError:
                continue

        age_days = (now - last_updated).days
        if age_days >= days_old:
            stale_files.append((rel_path, age_days, last_updated.strftime("%Y-%m-%d")))

    if not stale_files:
        return finalize_tracked_call(
            "tool",
            "find_stale_notes",
            started_at,
            {"days_old": days_old},
            f"Good job! No notes older than {days_old} days found.",
            meta={"days_old": days_old, "stale_count": 0},
        )

    stale_files.sort(key=lambda x: x[1], reverse=True)
    output = f"Found {len(stale_files)} notes older than {days_old} days:\n"
    output += "\n".join(f"- [{f[0]}] (Last updated: {f[2]}, {f[1]} days ago)" for f in stale_files[:30])
    if len(stale_files) > 30:
        output += f"\n... and {len(stale_files) - 30} more."
    return finalize_tracked_call(
        "tool",
        "find_stale_notes",
        started_at,
        {"days_old": days_old},
        output,
        meta={"days_old": days_old, "stale_count": len(stale_files)},
    )


@mcp.tool()
def find_unlinked_mentions(
    filepath_filter: str = "",
    filepath_filter_mode: str = "prefix",
    min_term_length: int = 3,
    max_results: int = 50,
) -> str:
    """Finds plain-text mentions of existing note titles that are not yet written as [[wikilinks]]."""
    started_at = time.perf_counter()
    normalized_min_term_length = max(min_term_length, 1)
    normalized_max_results = max(max_results, 0)
    filepath_filter_normalized = normalize_filepath_filter(filepath_filter)
    normalized_filter_mode = normalize_filepath_filter_mode(filepath_filter_mode)
    candidate_entries = collect_note_title_candidates(
        VAULT_PATH,
        IGNORED_FOLDERS,
        min_term_length=normalized_min_term_length,
    )
    results = []
    matched_file_count = 0
    scanned_file_count = 0
    scan_errors = []
    scan_error_count = 0
    max_scan_errors = 5

    for rel_path, file_path in iter_markdown_files(VAULT_PATH, IGNORED_FOLDERS):
        if not filepath_matches_filter(rel_path, filepath_filter_normalized, normalized_filter_mode):
            continue

        scanned_file_count += 1
        current_title_normalized = normalize_search_text(Path(rel_path).stem)
        file_results = []
        in_frontmatter = False
        frontmatter_checked = False
        in_fenced_block = False

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    stripped_line = line.strip()

                    if not frontmatter_checked:
                        frontmatter_checked = True
                        if stripped_line == "---":
                            in_frontmatter = True
                            continue

                    if in_frontmatter:
                        if stripped_line == "---":
                            in_frontmatter = False
                        continue

                    if FENCE_TOGGLE_PATTERN.match(line):
                        in_fenced_block = not in_fenced_block
                        continue

                    if in_fenced_block:
                        continue

                    sanitized_line = sanitize_line_for_unlinked_mentions(line)
                    if not sanitized_line.strip():
                        continue

                    occupied_ranges: list[tuple[int, int]] = []
                    for entry in candidate_entries:
                        if entry["normalized_title"] == current_title_normalized:
                            continue

                        matches = list(entry["pattern"].finditer(sanitized_line))
                        if not matches:
                            continue

                        for match in matches:
                            start, end = match.span()
                            if any(start < existing_end and end > existing_start for existing_start, existing_end in occupied_ranges):
                                continue

                            occupied_ranges.append((start, end))
                            file_results.append(
                                {
                                    "file": rel_path,
                                    "line": line_number,
                                    "mention_text": match.group(0),
                                    "note_title": entry["title"],
                                    "target_paths": entry["paths"],
                                    "ambiguous_target": len(entry["paths"]) > 1,
                                    "snippet_markdown": apply_masking(line.strip()),
                                }
                            )
                            break
        except Exception as exc:
            scan_error_count += 1
            if len(scan_errors) < max_scan_errors:
                scan_errors.append({"file": rel_path, "error_type": type(exc).__name__})
            continue

        if file_results:
            matched_file_count += 1
            results.extend(file_results)

    truncated_results = results[:normalized_max_results]
    response_payload = {
        "ok": True,
        "filepath_filter": filepath_filter_normalized or None,
        "filepath_filter_mode": normalized_filter_mode,
        "min_term_length": normalized_min_term_length,
        "scanned_file_count": scanned_file_count,
        "candidate_note_count": len(candidate_entries),
        "matched_file_count": matched_file_count,
        "result_count": len(truncated_results),
        "total_result_count": len(results),
        "results_truncated": len(results) > len(truncated_results),
        "scan_error_count": scan_error_count,
        "scan_errors_truncated": scan_error_count > len(scan_errors),
        "scan_errors": scan_errors,
        "results": truncated_results,
    }
    return finalize_tracked_call(
        "tool",
        "find_unlinked_mentions",
        started_at,
        {
            "filepath_filter": filepath_filter,
            "filepath_filter_mode": filepath_filter_mode,
            "min_term_length": min_term_length,
            "max_results": max_results,
        },
        response_payload,
        meta={
            "filepath_filter": filepath_filter_normalized or None,
            "filepath_filter_mode": normalized_filter_mode,
            "min_term_length": normalized_min_term_length,
            "max_results": normalized_max_results,
            "candidate_note_count": len(candidate_entries),
            "matched_file_count": matched_file_count,
            "match_count": len(results),
            "scanned_file_count": scanned_file_count,
            "scan_error_count": scan_error_count,
        },
    )


@mcp.prompt()
def note_format() -> str:
    """Fetch this formatting template before writing a new note."""
    started_at = time.perf_counter()
    template_path = BASE_DIR / "templates" / "note_template.md"
    try:
        template = read_text_file(template_path)
        result = (
            f"Please format all new notes exactly according to this template:\n\n{template}\n\n"
            f"CRITICAL INSTRUCTION: You MUST fill out the 'sources: []' YAML tag and the '## Sources' section at the bottom with explicit origin URLs or references. "
            f"This is required so the system can verify information for depreciation later.\n"
            f"CRITICAL INSTRUCTION 2: For existing notes, prefer append_to_note, insert_after_heading, replace_section, and update_frontmatter over full write_note overwrites. You MUST NOT delete old information silently. Add a '## Changelog / History' entry with the date of the change so historical knowledge is preserved.\n"
            f"CRITICAL INSTRUCTION 3: You MUST structure the note semantically using H2 ('##') blocks for entirely new sections. This strict hierarchy allows the LLM to navigate the note via 'read_note_section' later without consuming massive token limits.\n\n"
            f"CRITICAL INSTRUCTION 4: You MUST set the 'mcp_masking' frontmatter field thoughtfully. Use 'balanced' for most notes, 'clear' for notes that should be returned unmasked, and 'required' for clearly sensitive people, finance, or contract content.\n\n"
            f"CRITICAL INSTRUCTION 5: Prefer multiple small H3 subsections over large monolithic sections. If one subsection grows beyond roughly 20-30 lines, split it into another H3 with a precise title so retrieval stays token-efficient.\n\n"
            f"Current Date for Frontmatter: {datetime.now().strftime('%Y-%m-%d')}"
        )
        return finalize_tracked_call(
            "prompt",
            "note_format",
            started_at,
            {},
            result,
            meta={"template_chars": len(template)},
        )
    except Exception:
        return finalize_tracked_call(
            "prompt",
            "note_format",
            started_at,
            {},
            "Write the note in standard Markdown, starting with a YAML frontmatter. You MUST include original source URLs as references.",
            status="error",
        )


@mcp.tool()
def get_token_usage_report(
    days: int = 30,
    include_prompts: bool = False,
    tool_name: str = "",
    since_call_id: str = "",
) -> str:
    """Returns a Markdown telemetry report summarizing token usage and estimated savings by MCP tool."""
    started_at = time.perf_counter()
    summary = TRACKER.summarize_records(
        days=days,
        include_prompts=include_prompts,
        name_filter=tool_name,
        exclude_names={"get_token_usage_report", "write_token_usage_report_note"},
        since_call_id=since_call_id,
    )
    result = TRACKER.render_markdown_report(summary)
    return finalize_tracked_call(
        "tool",
        "get_token_usage_report",
        started_at,
        {
            "days": days,
            "include_prompts": include_prompts,
            "tool_name": tool_name,
            "since_call_id": since_call_id,
        },
        result,
        meta={
            "report_record_count": summary["record_count"],
            "filtered_tool": tool_name or None,
            "include_prompts": include_prompts,
            "since_call_id": since_call_id or None,
            "since_call_id_found": summary["since_call_id_found"],
        },
    )


@mcp.tool()
def write_token_usage_report_note(
    filepath: str = "00_Inbox/MCP Tool Usage Report.md",
    days: int = 30,
    include_prompts: bool = False,
    tool_name: str = "",
    since_call_id: str = "",
) -> str:
    """Writes a Markdown telemetry report into the vault so it can be viewed directly in Obsidian."""
    started_at = time.perf_counter()
    filepath, target_file = resolve_markdown_target(filepath)
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}

    validation_error = validate_markdown_write_target(filepath, target_file)
    if validation_error:
        return tracked_error(
            "tool",
            "write_token_usage_report_note",
            started_at,
            {
                "filepath": filepath,
                "days": days,
                "include_prompts": include_prompts,
                "tool_name": tool_name,
                "since_call_id": since_call_id,
            },
            validation_error.replace("Use 'list_notes' or 'search_vault' to find the correct directory.", ""),
            meta=meta,
        )

    summary = TRACKER.summarize_records(
        days=days,
        include_prompts=include_prompts,
        name_filter=tool_name,
        exclude_names={"get_token_usage_report", "write_token_usage_report_note"},
        since_call_id=since_call_id,
    )
    report = TRACKER.render_markdown_report(summary)

    write_text_file(target_file, report)

    return finalize_tracked_call(
        "tool",
        "write_token_usage_report_note",
        started_at,
        {
            "filepath": filepath,
            "days": days,
            "include_prompts": include_prompts,
            "tool_name": tool_name,
            "since_call_id": since_call_id,
        },
        f"Telemetry report written to '{filepath}'.",
        meta=meta | {
            "report_record_count": summary["record_count"],
            "filtered_tool": tool_name or None,
            "include_prompts": include_prompts,
            "since_call_id": since_call_id or None,
            "since_call_id_found": summary["since_call_id_found"],
            "report_chars": len(report),
        },
    )


if __name__ == "__main__":
    mcp.run()
