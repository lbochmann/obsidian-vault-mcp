import os
import shutil
import json
import re
import time
from datetime import datetime
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from telemetry import TokenTracker

PROTECTED_SEGMENT_TOKEN = "__MCP_PROTECTED_SEGMENT_"
FENCED_CODE_BLOCK_PATTERN = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?\n\2(?=\n|$)", re.DOTALL)
INLINE_CODE_PATTERN = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}]+")
VALID_MASKING_MODES = {"required", "balanced", "clear"}
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")

# Read configuration
CONFIG_PATH = Path(__file__).parent / "config.json"
with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

VAULT_PATH = (Path(__file__).parent / config.get("vault_path", "./test_vault")).resolve()
IGNORED_FOLDERS = config.get("ignored_folders", [".obsidian", ".git", ".trash"])

# Read privacy rules for masking
PRIVACY_PATH = Path(__file__).parent / "privacy_rules.json"
privacy_rules = []
if PRIVACY_PATH.exists():
    with open(PRIVACY_PATH, "r", encoding="utf-8") as f:
        privacy_data = json.load(f)
        privacy_rules = privacy_data.get("rules", [])

# ----- Hybrid Search: Presidio NLP Integration -----
PRESIDIO_AVAILABLE = False
try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine
    PRESIDIO_AVAILABLE = True
except ImportError:
    pass

ANALYZER_ENGINE = None
ANONYMIZER_ENGINE = None

def get_presidio_engines():
    global ANALYZER_ENGINE, ANONYMIZER_ENGINE
    if not PRESIDIO_AVAILABLE:
        return None, None
    if ANALYZER_ENGINE is None:
        try:
            # We configure it for German (primary) and English, since the user is in the DACH region
            configuration = {
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": "de", "model_name": "de_core_news_lg"},
                    {"lang_code": "en", "model_name": "en_core_web_lg"}
                ]
            }
            provider = NlpEngineProvider(nlp_configuration=configuration)
            nlp_engine = provider.create_engine()
            ANALYZER_ENGINE = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["de", "en"])
            ANONYMIZER_ENGINE = AnonymizerEngine()
        except Exception as e:
            print(f"Warning: Presidio initialization failed (missing SpaCy model?): {e}")
            try:
                # Minimal fallback using default English model
                ANALYZER_ENGINE = AnalyzerEngine()
                ANONYMIZER_ENGINE = AnonymizerEngine()
            except Exception:
                pass
    return ANALYZER_ENGINE, ANONYMIZER_ENGINE

# Initialize MCP Server
mcp = FastMCP("Obsidian Second Brain")
TRACKER = TokenTracker.from_config(
    config=config,
    base_dir=Path(__file__).parent,
    vault_path=VAULT_PATH,
    ignored_folders=IGNORED_FOLDERS,
)


def finalize_tracked_call(
    kind: str,
    name: str,
    started_at: float,
    args: dict,
    result: str,
    *,
    meta: dict | None = None,
    baseline_result_tokens: int | None = None,
) -> str:
    TRACKER.log_call(
        kind=kind,
        name=name,
        args=args,
        result=result,
        duration_ms=(time.perf_counter() - started_at) * 1000,
        status="error" if result.startswith("Error:") else "ok",
        meta=meta,
        baseline_result_tokens=baseline_result_tokens,
    )
    return result

def is_safe_path(requested_path: str) -> bool:
    """Prevents Path Traversal outside the vault."""
    target_path = (VAULT_PATH / requested_path).resolve()
    return target_path.is_relative_to(VAULT_PATH)

def apply_masking(text: str) -> str:
    """Applies Regex filters to mask personally identifiable information before passing it to the LLM."""
    if not privacy_rules:
        return text
        
    masked_text = text
    for rule in privacy_rules:
        pattern = rule.get("pattern")
        replacement = rule.get("replacement")
        if pattern and replacement:
            # We ignore multi-line matches for simple redaction
            masked_text = re.sub(pattern, replacement, masked_text)
            
    return masked_text


def protect_special_segments(text: str) -> tuple[str, dict[str, str]]:
    """Temporarily replaces Markdown/code-heavy segments so Presidio only sees natural language."""
    protected_segments: dict[str, str] = {}

    def replace_match(match: re.Match[str]) -> str:
        placeholder = f"{PROTECTED_SEGMENT_TOKEN}{len(protected_segments)}__"
        protected_segments[placeholder] = match.group(0)
        return placeholder

    protected_text = FENCED_CODE_BLOCK_PATTERN.sub(replace_match, text)
    protected_text = INLINE_CODE_PATTERN.sub(replace_match, protected_text)
    protected_text = URL_PATTERN.sub(replace_match, protected_text)
    return protected_text, protected_segments


def restore_special_segments(text: str, protected_segments: dict[str, str]) -> str:
    restored_text = text
    for placeholder, original in protected_segments.items():
        restored_text = restored_text.replace(placeholder, original)
    return restored_text


def extract_frontmatter(text: str) -> dict[str, str]:
    """Parses a minimal YAML-style frontmatter block for simple string and list fields."""
    if not text.startswith("---\n"):
        return {}

    lines = text.splitlines()
    frontmatter: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip()
    return frontmatter


def get_masking_mode(text: str) -> str:
    mode = extract_frontmatter(text).get("mcp_masking", "balanced").strip().strip("'\"").lower()
    if mode in VALID_MASKING_MODES:
        return mode
    return "balanced"


def normalize_search_text(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def parse_markdown_heading(line: str) -> tuple[int, str] | None:
    match = HEADING_PATTERN.match(line.strip())
    if not match:
        return None
    level = len(match.group(1))
    title = match.group(2).strip()
    return level, title


def parse_heading_query(heading: str) -> tuple[int | None, str]:
    stripped = heading.strip()
    parsed_heading = parse_markdown_heading(stripped)
    if parsed_heading:
        level, title = parsed_heading
        return level, normalize_search_text(title)
    return None, normalize_search_text(stripped)


def build_search_snippet(lines: list[str], match_index: int, context_lines: int) -> tuple[str, int, int]:
    start_line = max(match_index - max(context_lines, 0), 0)
    end_line = min(match_index + max(context_lines, 0) + 1, len(lines))
    snippet_lines = []

    for idx in range(start_line, end_line):
        prefix = ">" if idx == match_index else " "
        snippet_lines.append(f"{prefix} {lines[idx].strip()}")

    snippet_text = "\n".join(snippet_lines)
    return apply_masking(snippet_text), start_line + 1, end_line


def collect_available_headings(lines: list[str]) -> list[str]:
    headings = []
    for line in lines:
        parsed_heading = parse_markdown_heading(line)
        if parsed_heading:
            level, title = parsed_heading
            headings.append(f"{'#' * level} {title}")
    return headings

def apply_deep_masking(text: str, masking_mode: str = "balanced") -> str:
    """Applies masking with per-note policy control for technical versus sensitive content."""
    masked_text = apply_masking(text)
    if masking_mode == "clear":
        return masked_text

    if masking_mode == "required":
        protected_text = masked_text
        protected_segments: dict[str, str] = {}
    else:
        protected_text, protected_segments = protect_special_segments(masked_text)
    
    analyzer, anonymizer = get_presidio_engines()
    if analyzer and anonymizer:
        try:
            results = analyzer.analyze(text=protected_text, language='de')
            if results:
                anonymized_result = anonymizer.anonymize(text=protected_text, analyzer_results=results)
                protected_text = anonymized_result.text
        except Exception as e:
            print(f"Warning: Presidio deep masking failed: {e}")
            
    return restore_special_segments(protected_text, protected_segments)

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
        )

    files = []
    for root, dirs, filenames in os.walk(target_dir):
        # Ignore hidden folders and configured folders
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]
        
        for name in filenames:
            if name.endswith(".md"):
                # Relative path from Vault Root
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
    """Returns the folder structure (directories only) of the Vault as a tree. Extremely useful to understand the categorization system without loading thousands of files."""
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
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}
    
    if not is_safe_path(filepath) or not target_file.exists():
        return finalize_tracked_call(
            "tool",
            "read_note",
            started_at,
            {"filepath": filepath},
            f"Error: File '{filepath}' not found.",
            meta=meta,
        )
        
    with open(target_file, "r", encoding="utf-8") as f:
        content = f.read()
    masking_mode = get_masking_mode(content)
        
    # Apply deep hybrid masking before data leaves the local server
    masked_content = apply_deep_masking(content, masking_mode=masking_mode)
    
    # Observability/System Telemetry: Inject warning into the context if Presidio failed
    analyzer, _ = get_presidio_engines()
    if not analyzer:
        warning_block = (
            "> [!WARNING] SYSTEM NOTE TO LLM\n"
            "> The Deep PII NLP Filter (Presidio) is currently unavailable or failed to load. "
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
        },
    )

@mcp.tool()
def search_vault(query: str, include_filenames: bool = True, context_lines: int = 1) -> str:
    """Searches note contents and, optionally, filenames across the vault."""
    started_at = time.perf_counter()
    query_raw = query
    query = query.lower()
    results = []
    matched_file_count = 0
    baseline_result_tokens = 0
    
    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]
        
        for name in filenames:
            if name.endswith(".md"):
                file_path = os.path.join(root, name)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    lines = content.splitlines()
                    file_had_match = False
                    rel_path = os.path.relpath(file_path, VAULT_PATH)

                    if include_filenames and query in rel_path.lower():
                        file_had_match = True
                        matched_file_count += 1
                        baseline_result_tokens += TRACKER.count_tokens(content)
                        results.append(f"[{rel_path}] Filename match")
                        
                    for i, line in enumerate(lines):
                        if query in line.lower():
                            if not file_had_match:
                                file_had_match = True
                                matched_file_count += 1
                                baseline_result_tokens += TRACKER.count_tokens(content)
                            snippet_masked, start_line, end_line = build_search_snippet(lines, i, context_lines)
                            if start_line == end_line:
                                line_label = f"Line {start_line}"
                            else:
                                line_label = f"Lines {start_line}-{end_line}"
                            results.append(f"[{rel_path}] {line_label}:\n{snippet_masked}")
                except Exception:
                    pass # Ignore read errors for binary or weird files
                    
    if not results:
        return finalize_tracked_call(
            "tool",
            "search_vault",
            started_at,
            {"query": query_raw, "include_filenames": include_filenames, "context_lines": context_lines},
            f"No matches found for '{query_raw}'.",
            meta={
                "query_length": len(query),
                "match_count": 0,
                "matched_file_count": 0,
                "include_filenames": include_filenames,
                "context_lines": context_lines,
            },
            baseline_result_tokens=0,
        )
        
    # Limit results to save tokens
    max_results = 20
    output = "\n".join(results[:max_results])
    if len(results) > max_results:
        output += f"\n... and {len(results) - max_results} more matches."
        
    result = f"Search results for '{query_raw}':\n\n{output}"
    return finalize_tracked_call(
        "tool",
        "search_vault",
        started_at,
        {"query": query_raw, "include_filenames": include_filenames, "context_lines": context_lines},
        result,
        meta={
            "query_length": len(query),
            "match_count": len(results),
            "matched_file_count": matched_file_count,
            "max_results": max_results,
            "include_filenames": include_filenames,
            "context_lines": context_lines,
            "baseline_strategy": "full_matched_files_raw",
        },
        baseline_result_tokens=baseline_result_tokens,
    )

@mcp.tool()
def get_note_outline(filepath: str) -> str:
    """Returns a semantic outline (table of contents) of a Markdown file by extracting its headers. Useful for token optimization."""
    started_at = time.perf_counter()
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}
    
    if not is_safe_path(filepath) or not target_file.exists():
        return finalize_tracked_call(
            "tool",
            "get_note_outline",
            started_at,
            {"filepath": filepath},
            f"Error: File '{filepath}' not found.",
            meta=meta,
        )
        
    with open(target_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    outline = []
    for line in lines:
        if line.startswith("#"):
            outline.append(line.strip())
            
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
def read_note_section(filepath: str, heading: str, offset_lines: int = 0, max_lines: int = 40) -> str:
    """Reads a specific section under a heading and can return it in smaller paginated chunks to save tokens."""
    started_at = time.perf_counter()
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    meta = {
        "filepath_ref": TRACKER.path_ref(filepath),
        "heading_query_ref": TRACKER.path_ref(normalize_search_text(heading)),
    }
    
    if not is_safe_path(filepath) or not target_file.exists():
        return finalize_tracked_call(
            "tool",
            "read_note_section",
            started_at,
            {"filepath": filepath, "heading": heading, "offset_lines": offset_lines, "max_lines": max_lines},
            f"Error: File '{filepath}' not found.",
            meta=meta,
        )
        
    with open(target_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    file_content = "".join(lines)
    masking_mode = get_masking_mode(file_content)
    available_headings = collect_available_headings(lines)
        
    section_lines = []
    in_section = False
    requested_level, target_heading = parse_heading_query(heading)

    if not target_heading:
        return finalize_tracked_call(
            "tool",
            "read_note_section",
            started_at,
            {"filepath": filepath, "heading": heading, "offset_lines": offset_lines, "max_lines": max_lines},
            "Error: Provided heading is empty.",
            meta=meta | {"heading_level": requested_level or 0},
        )

    matched_level = None

    for line in lines:
        parsed_heading = parse_markdown_heading(line)
        
        if not in_section:
            if not parsed_heading:
                continue
            current_level, current_title = parsed_heading
            current_heading = normalize_search_text(current_title)
            if current_heading == target_heading and (requested_level is None or current_level == requested_level):
                in_section = True
                matched_level = current_level
                section_lines.append(line)
        else:
            # Drop out of section if we hit a new header of equal or higher priority
            if parsed_heading:
                current_level, _ = parsed_heading
                if matched_level is not None and current_level <= matched_level:
                    break
                    
            section_lines.append(line)
            
    if not section_lines:
        available_preview = available_headings[:20]
        error_message = f"Error: Heading '{heading}' not found in file."
        if available_preview:
            error_message += "\nAvailable headings:\n" + "\n".join(f"- {item}" for item in available_preview)
            if len(available_headings) > len(available_preview):
                error_message += f"\n- ... and {len(available_headings) - len(available_preview)} more"
        return finalize_tracked_call(
            "tool",
            "read_note_section",
            started_at,
            {"filepath": filepath, "heading": heading, "offset_lines": offset_lines, "max_lines": max_lines},
            error_message,
            meta=meta | {
                "heading_level": requested_level or 0,
                "available_heading_count": len(available_headings),
            },
        )

    heading_line = section_lines[0]
    body_lines = section_lines[1:]
    normalized_offset = max(offset_lines, 0)

    if normalized_offset > len(body_lines):
        return finalize_tracked_call(
            "tool",
            "read_note_section",
            started_at,
            {"filepath": filepath, "heading": heading, "offset_lines": offset_lines, "max_lines": max_lines},
            f"Error: offset_lines {offset_lines} exceeds the section length of {len(body_lines)} body lines.",
            meta=meta | {
                "heading_level": matched_level or requested_level or 0,
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
    
    # We apply deep masking just like in the full read
    masked_content = apply_deep_masking(content, masking_mode=masking_mode)

    if truncated:
        masked_content += (
            "\n\n> [!NOTE] SECTION TRUNCATED\n"
            f"> Returned body lines {normalized_offset + 1}-{next_offset} of {len(body_lines)}. "
            f"Call `read_note_section` again with `offset_lines={next_offset}` to continue.\n"
        )
    
    analyzer, _ = get_presidio_engines()
    if not analyzer:
        warning_block = (
            "> [!WARNING] SYSTEM NOTE TO LLM\n"
            "> The Deep PII NLP Filter (Presidio) is currently unavailable or failed to load. "
            "> Only basic Regex domain masking was applied. "
            "> YOU MUST explicitly warn the user about this in your response.\n\n"
        )
        masked_content = warning_block + masked_content
        
    return finalize_tracked_call(
        "tool",
        "read_note_section",
        started_at,
        {"filepath": filepath, "heading": heading, "offset_lines": offset_lines, "max_lines": max_lines},
        masked_content,
        meta=meta | {
            "heading_level": matched_level or requested_level or 0,
            "section_chars": len(content),
            "section_total_body_lines": len(body_lines),
            "offset_lines": normalized_offset,
            "max_lines": max_lines,
            "returned_body_lines": len(chunk_body_lines),
            "truncated": truncated,
            "baseline_strategy": "full_note_raw",
            "presidio_available": bool(analyzer),
            "masking_mode": masking_mode,
        },
        baseline_result_tokens=TRACKER.count_tokens("".join(lines)),
    )

@mcp.tool()
def write_note(filepath: str, content: str) -> str:
    """Creates or overwrites a Markdown note. Please use the 'note_format' prompt beforehand for the correct layout."""
    started_at = time.perf_counter()
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    meta = {
        "filepath_ref": TRACKER.path_ref(filepath),
        "content_chars": len(content),
    }
    
    if not is_safe_path(filepath):
        return finalize_tracked_call(
            "tool",
            "write_note",
            started_at,
            {"filepath": filepath, "content": content},
            f"Error: Invalid path '{filepath}'.",
            meta=meta,
        )
        
    # Structural Security Check: Prevent the LLM from hallucinating new root-level folders.
    path_parts = Path(filepath).parts
    if len(path_parts) > 1:
        top_level_folder = VAULT_PATH / path_parts[0]
        if not top_level_folder.exists():
            return finalize_tracked_call(
                "tool",
                "write_note",
                started_at,
                {"filepath": filepath, "content": content},
                f"Error: Structural policy prevents creating new root-level folders ('{path_parts[0]}'). Please place the note in an existing folder like '00_Inbox'. Use 'list_notes' or 'search_vault' to find the correct directory.",
                meta=meta | {"top_level_folder": path_parts[0]},
            )
            
    # Create folder structure if it doesn't exist (only allows sub-folders inside existing root folders now)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(content)
        
    return finalize_tracked_call(
        "tool",
        "write_note",
        started_at,
        {"filepath": filepath, "content": content},
        f"Note successfully written: {filepath}",
        meta=meta,
    )

@mcp.tool()
def archive_note(filepath: str) -> str:
    """Moves a processed note from the Inbox or Clippings folder to the Archive folder to keep the workspace clean."""
    started_at = time.perf_counter()
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    source_file = (VAULT_PATH / filepath).resolve()
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}
    
    if not is_safe_path(filepath) or not source_file.exists():
        return finalize_tracked_call(
            "tool",
            "archive_note",
            started_at,
            {"filepath": filepath},
            f"Error: File '{filepath}' not found or invalid path.",
            meta=meta,
        )
        
    # Security Check: Only allow archiving from Inbox or Clippings
    rel_path = os.path.relpath(source_file, VAULT_PATH)
    if not (rel_path.startswith("00_Inbox") or rel_path.startswith("Clippings")):
        return finalize_tracked_call(
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
    
    # Avoid overwriting if a file with the same name already exists in the archive
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
    
    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]
        
        for name in filenames:
            if name.endswith(".md"):
                file_path = os.path.join(root, name)
                rel_path = os.path.relpath(file_path, VAULT_PATH)
                
                # Check parsed frontmatter 'updated:' or fallback to file mtime
                last_updated = None
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    # simplistic frontmatter parse
                    in_frontmatter = False
                    for line in lines[:20]: # check top 20 lines
                        if line.strip() == "---":
                            if not in_frontmatter:
                                in_frontmatter = True
                            else:
                                break
                        elif in_frontmatter and line.startswith("updated:"):
                            date_str = line.split("updated:")[1].strip()
                            try:
                                last_updated = datetime.strptime(date_str, "%Y-%m-%d")
                            except ValueError:
                                pass
                except Exception:
                    pass
                
                if not last_updated:
                    # fallback
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
        
    # Sort by oldest first
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

@mcp.prompt()
def note_format() -> str:
    """Fetch this formatting template before writing a new note."""
    started_at = time.perf_counter()
    template_path = Path(__file__).parent / "templates" / "note_template.md"
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
            result = (
                f"Please format all new notes exactly according to this template:\n\n{template}\n\n"
                f"CRITICAL INSTRUCTION: You MUST fill out the 'sources: []' YAML tag and the '## Sources' section at the bottom with explicit origin URLs or references. "
                f"This is required so the system can verify information for depreciation later.\n"
                f"CRITICAL INSTRUCTION 2: When updating an existing note via write_note, you MUST NOT delete the old information entirely. Instead, summarize the old state and add it to the '## Changelog / History' section, along with the date of the change, so historical knowledge is preserved.\n"
                f"CRITICAL INSTRUCTION 3: You MUST structure the note semantically using H2 ('##') blocks for entirely new sections. This strict hierarchy allows the LLM to navigate the note via 'read_note_section' later without consuming massive token limits.\n\n"
                f"CRITICAL INSTRUCTION 4: You MUST set the 'mcp_masking' frontmatter field thoughtfully. Use 'balanced' for most notes, 'clear' for purely technical code-heavy notes, and 'required' for clearly sensitive people, finance, or contract content.\n\n"
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
        )

@mcp.tool()
def get_token_usage_report(days: int = 30, include_prompts: bool = False, tool_name: str = "") -> str:
    """Returns a Markdown telemetry report summarizing token usage and estimated savings by MCP tool."""
    started_at = time.perf_counter()
    summary = TRACKER.summarize_records(
        days=days,
        include_prompts=include_prompts,
        name_filter=tool_name,
        exclude_names={"get_token_usage_report", "write_token_usage_report_note"},
    )
    result = TRACKER.render_markdown_report(summary)
    return finalize_tracked_call(
        "tool",
        "get_token_usage_report",
        started_at,
        {"days": days, "include_prompts": include_prompts, "tool_name": tool_name},
        result,
        meta={
            "report_record_count": summary["record_count"],
            "filtered_tool": tool_name or None,
            "include_prompts": include_prompts,
        },
    )

@mcp.tool()
def write_token_usage_report_note(
    filepath: str = "00_Inbox/MCP Tool Usage Report.md",
    days: int = 30,
    include_prompts: bool = False,
    tool_name: str = "",
) -> str:
    """Writes a Markdown telemetry report into the vault so it can be viewed directly in Obsidian."""
    started_at = time.perf_counter()
    if not filepath.endswith(".md"):
        filepath += ".md"

    target_file = (VAULT_PATH / filepath).resolve()
    meta = {"filepath_ref": TRACKER.path_ref(filepath)}

    if not is_safe_path(filepath):
        return finalize_tracked_call(
            "tool",
            "write_token_usage_report_note",
            started_at,
            {"filepath": filepath, "days": days, "include_prompts": include_prompts, "tool_name": tool_name},
            f"Error: Invalid path '{filepath}'.",
            meta=meta,
        )

    path_parts = Path(filepath).parts
    if len(path_parts) > 1:
        top_level_folder = VAULT_PATH / path_parts[0]
        if not top_level_folder.exists():
            return finalize_tracked_call(
                "tool",
                "write_token_usage_report_note",
                started_at,
                {"filepath": filepath, "days": days, "include_prompts": include_prompts, "tool_name": tool_name},
                f"Error: Structural policy prevents creating new root-level folders ('{path_parts[0]}'). Please place the report in an existing folder like '00_Inbox'.",
                meta=meta | {"top_level_folder": path_parts[0]},
            )

    summary = TRACKER.summarize_records(
        days=days,
        include_prompts=include_prompts,
        name_filter=tool_name,
        exclude_names={"get_token_usage_report", "write_token_usage_report_note"},
    )
    report = TRACKER.render_markdown_report(summary)

    target_file.parent.mkdir(parents=True, exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(report)

    return finalize_tracked_call(
        "tool",
        "write_token_usage_report_note",
        started_at,
        {"filepath": filepath, "days": days, "include_prompts": include_prompts, "tool_name": tool_name},
        f"Telemetry report written to '{filepath}'.",
        meta=meta | {
            "report_record_count": summary["record_count"],
            "filtered_tool": tool_name or None,
            "include_prompts": include_prompts,
            "report_chars": len(report),
        },
    )

if __name__ == "__main__":
    mcp.run()
