import os
import shutil
import json
import re
import time
from fnmatch import fnmatch
from difflib import SequenceMatcher
from datetime import datetime
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from telemetry import TokenTracker

PROTECTED_SEGMENT_TOKEN = "__MCP_PROTECTED_SEGMENT_"
FENCED_CODE_BLOCK_PATTERN = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?\n\2(?=\n|$)", re.DOTALL)
INLINE_CODE_PATTERN = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}]+")
WIKILINK_PATTERN = re.compile(r"\[\[[^\]]+\]\]")
WIKILINK_CAPTURE_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")
VALID_MASKING_MODES = {"required", "balanced", "clear"}
VALID_WRITE_MODES = {"overwrite", "create_only", "append"}
VALID_FILEPATH_FILTER_MODES = {"prefix", "substring", "glob"}
VALID_INSERT_PLACEMENTS = {"end_of_section", "after_heading"}
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
FENCE_TOGGLE_PATTERN = re.compile(r"^\s*(```|~~~)")
SUPPORTED_PRESIDIO_LANGUAGES = {
    "de": "de_core_news_lg",
    "en": "en_core_web_lg",
}

def load_json_file(preferred_name: str, fallback_name: str) -> tuple[dict, Path]:
    """Load a local JSON file, falling back to the example file in fresh clones."""
    base_dir = Path(__file__).parent
    preferred_path = base_dir / preferred_name
    fallback_path = base_dir / fallback_name

    if preferred_path.exists():
        with open(preferred_path, "r", encoding="utf-8") as f:
            return json.load(f), preferred_path

    with open(fallback_path, "r", encoding="utf-8") as f:
        return json.load(f), fallback_path


config, CONFIG_PATH = load_json_file("config.json", "config.example.json")

VAULT_PATH = (Path(__file__).parent / config.get("vault_path", "./test_vault")).resolve()
IGNORED_FOLDERS = config.get("ignored_folders", [".obsidian", ".git", ".trash"])
privacy_config = config.get("privacy", {})
NLP_LANGUAGE = str(privacy_config.get("nlp_language", "de")).strip().lower()
if NLP_LANGUAGE not in SUPPORTED_PRESIDIO_LANGUAGES:
    NLP_LANGUAGE = "de"
PRESIDIO_MODEL = SUPPORTED_PRESIDIO_LANGUAGES[NLP_LANGUAGE]

privacy_data, PRIVACY_PATH = load_json_file("privacy_rules.json", "privacy_rules.example.json")
privacy_rules = privacy_data.get("rules", [])

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
            configuration = {
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": NLP_LANGUAGE, "model_name": PRESIDIO_MODEL}
                ]
            }
            provider = NlpEngineProvider(nlp_configuration=configuration)
            nlp_engine = provider.create_engine()
            ANALYZER_ENGINE = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=[NLP_LANGUAGE])
            ANONYMIZER_ENGINE = AnonymizerEngine()
        except Exception as e:
            print(f"Warning: Presidio initialization failed (missing SpaCy model?): {e}")
            if NLP_LANGUAGE == "en":
                try:
                    ANALYZER_ENGINE = AnalyzerEngine()
                    ANONYMIZER_ENGINE = AnonymizerEngine()
                except Exception:
                    pass
    return ANALYZER_ENGINE, ANONYMIZER_ENGINE

mcp = FastMCP("obsidian-vault-mcp")
TRACKER = TokenTracker.from_config(
    config=config,
    base_dir=Path(__file__).parent,
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
    """Serialize a tool result, write telemetry, and return the serialized payload."""
    serialized_result = serialize_tool_result(result)
    TRACKER.log_call(
        kind=kind,
        name=name,
        args=args,
        result=serialized_result,
        duration_ms=(time.perf_counter() - started_at) * 1000,
        status=status,
        meta=meta,
        baseline_result_tokens=baseline_result_tokens,
    )
    return serialized_result


def serialize_tool_result(payload) -> str:
    """Return plain strings unchanged and serialize structured payloads as JSON."""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, indent=2)

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
    """Record a tracked error result with consistent telemetry semantics."""
    return finalize_tracked_call(
        kind,
        name,
        started_at,
        args,
        result,
        meta=meta,
        baseline_result_tokens=baseline_result_tokens,
        status="error",
    )

def is_safe_path(requested_path: str) -> bool:
    """Prevents Path Traversal outside the vault."""
    vault_root = VAULT_PATH.resolve()
    target_path = (vault_root / requested_path).resolve()
    return target_path.is_relative_to(vault_root)

def normalize_markdown_filepath(filepath: str) -> str:
    """Ensure note-oriented tools consistently operate on Markdown file paths."""
    if filepath.endswith(".md"):
        return filepath
    return f"{filepath}.md"

def resolve_vault_target(filepath: str) -> Path:
    """Resolve a vault-relative path to an absolute local filesystem path."""
    return (VAULT_PATH / filepath).resolve()

def resolve_markdown_target(filepath: str) -> tuple[str, Path]:
    """Normalize a note path and resolve it inside the configured vault."""
    normalized_filepath = normalize_markdown_filepath(filepath)
    return normalized_filepath, resolve_vault_target(normalized_filepath)


def validate_markdown_write_target(filepath: str, target_file: Path) -> str | None:
    """Return an error string when a Markdown write would violate vault policy."""
    if not is_safe_path(filepath):
        return f"Error: Invalid path '{filepath}'."

    path_parts = Path(filepath).parts
    if len(path_parts) > 1:
        top_level_folder = VAULT_PATH / path_parts[0]
        if not top_level_folder.exists():
            return (
                f"Error: Structural policy prevents creating new root-level folders ('{path_parts[0]}'). "
                "Please place the note in an existing folder like '00_Inbox'. Use 'list_notes' or "
                "'search_vault' to find the correct directory."
            )

    if not target_file.resolve().is_relative_to(VAULT_PATH.resolve()):
        return f"Error: Invalid path '{filepath}'."

    return None


def read_text_file(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def append_markdown_block(existing_content: str, content: str) -> str:
    """Append a Markdown block with predictable spacing and a final newline."""
    block = content.strip("\n")
    if not block:
        return existing_content

    separator = ""
    if existing_content:
        if not existing_content.endswith("\n"):
            separator = "\n\n"
        elif not existing_content.endswith("\n\n"):
            separator = "\n"

    return f"{existing_content}{separator}{block}\n"


def join_markdown_parts(before: str, content: str, after: str) -> str:
    """Insert or replace a Markdown block while keeping neighboring sections readable."""
    block = content.strip("\n")
    if not block:
        return before + after

    if before and not before.endswith("\n"):
        before += "\n"
    if before and not before.endswith("\n\n"):
        before += "\n"

    result = before + block
    if not result.endswith("\n"):
        result += "\n"
    if after and not result.endswith("\n\n"):
        result += "\n"
    return result + after

def apply_masking(text: str) -> str:
    """Applies Regex filters to mask personally identifiable information before passing it to the LLM."""
    if not privacy_rules:
        return text
        
    masked_text = text
    for rule in privacy_rules:
        pattern = rule.get("pattern")
        replacement = rule.get("replacement")
        if pattern and replacement:
            # Keep the fast path simple and line-oriented.
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


def normalize_filepath_filter(filepath_filter: str) -> str:
    normalized = filepath_filter.strip().replace("\\", "/").lstrip("/")
    return normalized


def filepath_matches_filter(rel_path: str, filepath_filter: str, filter_mode: str) -> bool:
    if not filepath_filter:
        return True

    rel_path_lower = rel_path.replace("\\", "/").lower()
    filepath_filter_lower = filepath_filter.lower()

    if filter_mode == "substring":
        return filepath_filter_lower in rel_path_lower
    if filter_mode == "glob":
        return fnmatch(rel_path_lower, filepath_filter_lower)
    return rel_path_lower.startswith(filepath_filter_lower)


def sanitize_line_for_unlinked_mentions(line: str) -> str:
    sanitized = WIKILINK_PATTERN.sub(" ", line)
    sanitized = INLINE_CODE_PATTERN.sub(" ", sanitized)
    return sanitized


def build_note_title_pattern(title: str) -> re.Pattern[str]:
    escaped_title = re.escape(title)
    return re.compile(rf"(?<!\w){escaped_title}(?!\w)", re.IGNORECASE)


def is_meaningful_note_title(title: str, min_term_length: int) -> bool:
    compact_title = re.sub(r"[\W_]+", "", title.casefold())
    return len(compact_title) >= min_term_length


def collect_note_title_candidates(min_term_length: int) -> list[dict]:
    candidates_by_key: dict[str, dict] = {}

    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]

        for name in filenames:
            if not name.endswith(".md"):
                continue

            rel_path = os.path.relpath(os.path.join(root, name), VAULT_PATH)
            title = Path(name).stem.strip()
            normalized_title = normalize_search_text(title)
            if not normalized_title or not is_meaningful_note_title(title, min_term_length):
                continue

            entry = candidates_by_key.setdefault(
                normalized_title,
                {
                    "title": title,
                    "normalized_title": normalized_title,
                    "paths": [],
                },
            )
            entry["title"] = min(entry["title"], title, key=lambda value: (len(value), value.casefold(), value))
            entry["paths"].append(rel_path)

    candidates = []
    for key in sorted(candidates_by_key):
        entry = candidates_by_key[key]
        paths = sorted(set(entry["paths"]))
        title = entry["title"]
        candidates.append(
            {
                "title": title,
                "normalized_title": key,
                "paths": paths,
                "pattern": build_note_title_pattern(title),
            }
        )

    candidates.sort(key=lambda entry: (-len(entry["title"]), entry["title"].casefold(), entry["title"]))
    return candidates


def normalize_wikilink_target(target: str) -> str:
    link_target = target.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/").lstrip("/")
    if link_target.endswith(".md"):
        link_target = link_target[:-3]
    return normalize_search_text(link_target)


def wikilink_aliases_for_path(filepath: str) -> set[str]:
    normalized = normalize_markdown_filepath(filepath).replace("\\", "/").lstrip("/")
    no_extension = normalized[:-3] if normalized.endswith(".md") else normalized
    stem = Path(no_extension).name
    return {
        normalize_wikilink_target(no_extension),
        normalize_wikilink_target(stem),
    }


def resolve_backlink_aliases(filepath_or_title: str) -> set[str]:
    normalized_input = filepath_or_title.strip().replace("\\", "/").lstrip("/")
    if not normalized_input:
        return set()

    path_like = normalized_input.endswith(".md") or "/" in normalized_input
    if path_like:
        return wikilink_aliases_for_path(normalized_input)

    aliases = {normalize_wikilink_target(normalized_input)}
    candidate_path = normalize_markdown_filepath(normalized_input)
    candidate_file = resolve_vault_target(candidate_path)
    if candidate_file.exists():
        aliases.update(wikilink_aliases_for_path(candidate_path))
    return aliases


def iter_markdown_files() -> list[tuple[str, Path]]:
    files = []
    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]
        for name in filenames:
            if name.endswith(".md"):
                file_path = Path(root) / name
                rel_path = os.path.relpath(file_path, VAULT_PATH)
                files.append((rel_path, file_path))
    return files


def build_wikilink_replacement(match: re.Match[str], old_aliases: set[str], new_filepath: str) -> str:
    raw_inner = match.group(1)
    target_and_heading, alias = (raw_inner.split("|", 1) + [""])[:2] if "|" in raw_inner else (raw_inner, "")
    target, heading = (target_and_heading.split("#", 1) + [""])[:2] if "#" in target_and_heading else (target_and_heading, "")

    if normalize_wikilink_target(target) not in old_aliases:
        return match.group(0)

    new_no_extension = normalize_markdown_filepath(new_filepath).replace("\\", "/").lstrip("/")
    if new_no_extension.endswith(".md"):
        new_no_extension = new_no_extension[:-3]

    old_target_normalized = normalize_wikilink_target(target)
    new_stem = Path(new_no_extension).name
    replacement_target = new_stem if old_target_normalized == normalize_wikilink_target(Path(target).name) and "/" not in target else new_no_extension

    if heading:
        replacement_target += f"#{heading}"
    if alias:
        replacement_target += f"|{alias}"
    return f"[[{replacement_target}]]"


def collect_available_headings(lines: list[str]) -> list[str]:
    headings = []
    for line in lines:
        parsed_heading = parse_markdown_heading(line)
        if parsed_heading:
            level, title = parsed_heading
            headings.append(f"{'#' * level} {title}")
    return headings


def find_best_heading_match(
    lines: list[str],
    target_heading: str,
    requested_level: int | None,
) -> tuple[int | None, int | None, str | None, float]:
    """Return the closest heading match when the caller enables fuzzy resolution."""
    best_index = None
    best_level = None
    best_line = None
    best_score = 0.0

    for idx, line in enumerate(lines):
        parsed_heading = parse_markdown_heading(line)
        if not parsed_heading:
            continue
        current_level, current_title = parsed_heading
        if requested_level is not None and current_level != requested_level:
            continue

        normalized_title = normalize_search_text(current_title)
        if not normalized_title:
            continue

        score = SequenceMatcher(None, target_heading, normalized_title).ratio()
        if target_heading in normalized_title or normalized_title in target_heading:
            score += 0.2

        if score > best_score:
            best_index = idx
            best_level = current_level
            best_line = line
            best_score = score

    return best_index, best_level, best_line, min(best_score, 1.0)


def section_end_index(lines: list[str], start_index: int, matched_level: int) -> int:
    for idx in range(start_index + 1, len(lines)):
        parsed_heading = parse_markdown_heading(lines[idx])
        if parsed_heading:
            current_level, _ = parsed_heading
            if current_level <= matched_level:
                return idx
    return len(lines)


def find_section_bounds(
    lines: list[str],
    heading: str,
    *,
    heading_fuzzy: bool = False,
) -> dict:
    requested_level, target_heading = parse_heading_query(heading)
    available_headings = collect_available_headings(lines)

    if not target_heading:
        return {
            "ok": False,
            "error": "Provided heading is empty.",
            "available_headings": available_headings,
        }

    exact_matches = []
    for idx, line in enumerate(lines):
        parsed_heading = parse_markdown_heading(line)
        if not parsed_heading:
            continue

        current_level, current_title = parsed_heading
        current_heading = normalize_search_text(current_title)
        if current_heading == target_heading and (requested_level is None or current_level == requested_level):
            exact_matches.append((idx, current_level, line))

    if len(exact_matches) > 1:
        return {
            "ok": False,
            "error": f"Heading '{heading}' matched {len(exact_matches)} sections. Include the heading level or rename duplicates before editing.",
            "available_headings": available_headings[:20],
            "available_heading_count": len(available_headings),
            "available_headings_truncated": len(available_headings) > 20,
        }

    if exact_matches:
        start_index, matched_level, heading_line = exact_matches[0]
        return {
            "ok": True,
            "start": start_index,
            "end": section_end_index(lines, start_index, matched_level),
            "heading_level": matched_level,
            "resolved_heading": heading_line.strip(),
            "match_strategy": "exact",
            "match_confidence": 1.0,
        }

    if heading_fuzzy:
        best_index, best_level, best_line, best_score = find_best_heading_match(lines, target_heading, requested_level)
        if best_index is not None and best_level is not None and best_line is not None and best_score >= 0.72:
            return {
                "ok": True,
                "start": best_index,
                "end": section_end_index(lines, best_index, best_level),
                "heading_level": best_level,
                "resolved_heading": best_line.strip(),
                "match_strategy": "fuzzy",
                "match_confidence": round(best_score, 4),
            }

    return {
        "ok": False,
        "error": f"Heading '{heading}' not found in file.",
        "available_headings": available_headings[:20],
        "available_heading_count": len(available_headings),
        "available_headings_truncated": len(available_headings) > 20,
    }


def split_frontmatter_block(text: str) -> tuple[list[str], list[str], bool]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return [], lines, False

    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return lines[1:idx], lines[idx + 1:], True

    return [], lines, False


def format_frontmatter_value(value) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if re.match(r"^[A-Za-z0-9_./@+-]+$", stripped):
            return stripped
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def update_frontmatter_content(text: str, updates: dict) -> str:
    frontmatter_lines, body_lines, has_frontmatter = split_frontmatter_block(text)
    mutable_lines = list(frontmatter_lines)
    key_to_index = {}

    for idx, line in enumerate(mutable_lines):
        match = re.match(r"^([A-Za-z0-9_-]+):", line)
        if match:
            key_to_index[match.group(1)] = idx

    for key, value in updates.items():
        key_text = str(key).strip()
        if not key_text or not re.match(r"^[A-Za-z0-9_-]+$", key_text):
            raise ValueError(f"Invalid frontmatter key '{key}'.")

        rendered_line = f"{key_text}: {format_frontmatter_value(value)}\n"
        if key_text in key_to_index:
            mutable_lines[key_to_index[key_text]] = rendered_line
        else:
            key_to_index[key_text] = len(mutable_lines)
            mutable_lines.append(rendered_line)

    if not has_frontmatter:
        body = "".join(body_lines)
        if body and not body.startswith("\n"):
            body = "\n" + body
        return "---\n" + "".join(mutable_lines) + "---\n" + body

    return "---\n" + "".join(mutable_lines) + "---\n" + "".join(body_lines)

def apply_deep_masking(text: str, masking_mode: str = "balanced") -> str:
    """Applies masking with per-note policy control for technical versus sensitive content."""
    if masking_mode == "clear":
        return text

    masked_text = apply_masking(text)

    if masking_mode == "required":
        protected_text = masked_text
        protected_segments: dict[str, str] = {}
    else:
        protected_text, protected_segments = protect_special_segments(masked_text)
    
    analyzer, anonymizer = get_presidio_engines()
    if analyzer and anonymizer:
        try:
            results = analyzer.analyze(text=protected_text, language=NLP_LANGUAGE)
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
            status="error",
        )

    files = []
    for root, dirs, filenames in os.walk(target_dir):
        # Skip hidden folders and configured exclusions.
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
        
    with open(target_file, "r", encoding="utf-8") as f:
        content = f.read()
    masking_mode = get_masking_mode(content)
        
    # Apply the note-aware masking policy before content leaves the server.
    masked_content = apply_deep_masking(content, masking_mode=masking_mode)
    
    # Surface degraded privacy filtering directly in the returned context.
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
    normalized_filter_mode = filepath_filter_mode.strip().lower()
    if normalized_filter_mode not in VALID_FILEPATH_FILTER_MODES:
        normalized_filter_mode = "prefix"
    results = []
    matched_file_count = 0
    baseline_result_tokens = 0
    scan_errors = []
    scan_error_count = 0
    max_scan_errors = 5
    
    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]
        
        for name in filenames:
            if name.endswith(".md"):
                file_path = os.path.join(root, name)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    lines = content.splitlines()
                    rel_path = os.path.relpath(file_path, VAULT_PATH)
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
                                "file": os.path.relpath(file_path, VAULT_PATH),
                                "error_type": type(exc).__name__,
                            }
                        )
                    
    if not results:
        return finalize_tracked_call(
            "tool",
            "search_vault",
            started_at,
            {
                "query": query_raw,
                "include_filenames": include_filenames,
                "context_lines": context_lines,
                "filepath_filter": filepath_filter,
                "filepath_filter_mode": filepath_filter_mode,
            },
            {
                "ok": True,
                "query": query_raw,
                "filepath_filter": filepath_filter_normalized or None,
                "filepath_filter_mode": normalized_filter_mode,
                "include_filenames": include_filenames,
                "context_lines": context_lines,
                "matched_file_count": 0,
                "result_count": 0,
                "total_result_count": 0,
                "results_truncated": False,
                "scan_error_count": scan_error_count,
                "scan_errors_truncated": scan_error_count > len(scan_errors),
                "scan_errors": scan_errors,
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
        
    # Cap search results to keep the response bounded.
    max_results = 20
    truncated_results = results[:max_results]
    result = {
        "ok": True,
        "query": query_raw,
        "filepath_filter": filepath_filter_normalized or None,
        "filepath_filter_mode": normalized_filter_mode,
        "include_filenames": include_filenames,
        "context_lines": context_lines,
        "matched_file_count": matched_file_count,
        "result_count": len(truncated_results),
        "total_result_count": len(results),
        "results_truncated": len(results) > max_results,
        "scan_error_count": scan_error_count,
        "scan_errors_truncated": scan_error_count > len(scan_errors),
        "scan_errors": scan_errors,
        "results": truncated_results,
    }
    return finalize_tracked_call(
        "tool",
        "search_vault",
        started_at,
        {
            "query": query_raw,
            "include_filenames": include_filenames,
            "context_lines": context_lines,
            "filepath_filter": filepath_filter,
            "filepath_filter_mode": filepath_filter_mode,
        },
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
    """Returns a semantic outline (table of contents) of a Markdown file by extracting its headers. Useful for token optimization."""
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
            {
                "ok": False,
                "error": f"File '{filepath}' not found.",
            },
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
        return tracked_error(
            "tool",
            "read_note_section",
            started_at,
            request_args,
            {
                "ok": False,
                "error": "Provided heading is empty.",
            },
            meta=meta | {"heading_level": requested_level or 0},
        )

    matched_level = None
    match_strategy = "exact"
    match_confidence = 1.0

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
            # Stop once a sibling or higher-level heading starts a new section.
            if parsed_heading:
                current_level, _ = parsed_heading
                if matched_level is not None and current_level <= matched_level:
                    break
                    
            section_lines.append(line)

    if not section_lines and heading_fuzzy:
        best_index, best_level, best_line, best_score = find_best_heading_match(lines, target_heading, requested_level)
        if best_index is not None and best_line is not None and best_score >= 0.72:
            match_strategy = "fuzzy"
            match_confidence = best_score
            matched_level = best_level
            section_lines = [lines[best_index]]
            for line in lines[best_index + 1:]:
                parsed_heading = parse_markdown_heading(line)
                if parsed_heading:
                    current_level, _ = parsed_heading
                    if matched_level is not None and current_level <= matched_level:
                        break
                section_lines.append(line)
            
    if not section_lines:
        available_preview = available_headings[:20]
        return tracked_error(
            "tool",
            "read_note_section",
            started_at,
            request_args,
            {
                "ok": False,
                "error": f"Heading '{heading}' not found in file.",
                "requested_heading": heading,
                "available_headings": available_preview,
                "available_heading_count": len(available_headings),
                "available_headings_truncated": len(available_headings) > len(available_preview),
            },
            meta=meta | {
                "heading_level": requested_level or 0,
                "available_heading_count": len(available_headings),
                "heading_fuzzy": heading_fuzzy,
            },
        )

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
    
    # Use the same masking path as full-note reads.
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
        "resolved_heading": heading_line.strip(),
        "heading_level": matched_level or requested_level or 0,
        "match_strategy": match_strategy,
        "match_confidence": round(match_confidence, 4),
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
            "heading_level": matched_level or requested_level or 0,
            "section_chars": len(content),
            "section_total_body_lines": len(body_lines),
            "offset_lines": normalized_offset,
            "max_lines": max_lines,
            "returned_body_lines": len(chunk_body_lines),
            "truncated": truncated,
            "heading_fuzzy": heading_fuzzy,
            "match_strategy": match_strategy,
            "match_confidence": round(match_confidence, 4),
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
        meta=meta | {
            "source_chars": len(existing_content),
            "result_chars": len(updated_content),
        },
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
        return tracked_error(
            "tool",
            "insert_after_heading",
            started_at,
            request_args,
            bounds,
            meta=meta,
        )

    insert_index = bounds["start"] + 1 if normalized_placement == "after_heading" else bounds["end"]
    before = "".join(lines[:insert_index])
    after = "".join(lines[insert_index:])
    updated_content = join_markdown_parts(before, content, after)
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
def replace_section(
    filepath: str,
    heading: str,
    new_content: str,
    heading_fuzzy: bool = False,
) -> str:
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
        return tracked_error(
            "tool",
            "replace_section",
            started_at,
            request_args,
            bounds,
            meta=meta,
        )

    replacement = new_content.strip("\n")
    replacement_first_line = replacement.splitlines()[0] if replacement else ""
    if not parse_markdown_heading(replacement_first_line):
        replacement = f"{bounds['resolved_heading']}\n\n{replacement}".rstrip("\n")

    before = "".join(lines[:bounds["start"]])
    after = "".join(lines[bounds["end"]:])
    updated_content = join_markdown_parts(before, replacement, after)
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
    meta = {
        "filepath_ref": TRACKER.path_ref(filepath),
        "content_chars": len(content),
        "mode": normalized_mode,
    }
    
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
        existing_content = read_text_file(target_file)
        final_content = append_markdown_block(existing_content, content)
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
    normalized_filter_mode = filepath_filter_mode.strip().lower()
    if normalized_filter_mode not in VALID_FILEPATH_FILTER_MODES:
        normalized_filter_mode = "prefix"

    target_aliases = resolve_backlink_aliases(filepath_or_title)
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

    results = []
    matched_file_count = 0
    scanned_file_count = 0
    scan_errors = []
    scan_error_count = 0
    max_scan_errors = 5

    for rel_path, file_path in iter_markdown_files():
        if not filepath_matches_filter(rel_path, filepath_filter_normalized, normalized_filter_mode):
            continue

        scanned_file_count += 1
        file_had_match = False
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    for match in WIKILINK_CAPTURE_PATTERN.finditer(line):
                        if normalize_wikilink_target(match.group(1)) not in target_aliases:
                            continue

                        file_had_match = True
                        results.append(
                            {
                                "file": rel_path,
                                "line": line_number,
                                "link_text": match.group(0),
                                "link_target": match.group(1).split("|", 1)[0].strip(),
                                "snippet_markdown": apply_masking(line.strip()),
                            }
                        )
        except Exception as exc:
            scan_error_count += 1
            if len(scan_errors) < max_scan_errors:
                scan_errors.append({"file": rel_path, "error_type": type(exc).__name__})
            continue

        if file_had_match:
            matched_file_count += 1

    response_payload = {
        "ok": True,
        "filepath_or_title": filepath_or_title,
        "filepath_filter": filepath_filter_normalized or None,
        "filepath_filter_mode": normalized_filter_mode,
        "matched_file_count": matched_file_count,
        "scanned_file_count": scanned_file_count,
        "result_count": len(results),
        "scan_error_count": scan_error_count,
        "scan_errors_truncated": scan_error_count > len(scan_errors),
        "scan_errors": scan_errors,
        "results": results,
    }
    return finalize_tracked_call(
        "tool",
        "find_backlinks",
        started_at,
        request_args,
        response_payload,
        meta=meta | {
            "matched_file_count": matched_file_count,
            "result_count": len(results),
            "scanned_file_count": scanned_file_count,
            "scan_error_count": scan_error_count,
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
        return tracked_error(
            "tool",
            "move_note",
            started_at,
            request_args,
            {"ok": False, "error": validation_error},
            meta=meta,
        )

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
        rel_path for rel_path, _ in iter_markdown_files()
        if rel_path != source_filepath and normalize_search_text(Path(rel_path).stem) == normalize_search_text(Path(source_filepath).stem)
    ]

    if update_links and duplicate_title_paths:
        for rel_path, file_path in iter_markdown_files():
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
        for rel_path, file_path in iter_markdown_files():
            try:
                content = read_text_file(file_path)
            except Exception:
                continue

            replacement_count = 0

            def replace_match(match: re.Match[str]) -> str:
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
        meta=meta | {
            "updated_link_count": updated_link_count,
            "updated_link_file_count": len(updated_link_files),
        },
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
        
    # Restrict archiving to the expected intake folders.
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
    
    # Keep archived filenames unique without overwriting existing notes.
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
                
                last_updated = None
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    updated_value = extract_frontmatter(content).get("updated", "").strip().strip("'\"")
                    if updated_value:
                        try:
                            last_updated = datetime.strptime(updated_value, "%Y-%m-%d")
                        except ValueError:
                            pass
                except Exception:
                    pass
                
                if not last_updated:
                    # Fall back to filesystem timestamps when frontmatter is missing.
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
        
    # Show the oldest notes first.
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
    normalized_filter_mode = filepath_filter_mode.strip().lower()
    if normalized_filter_mode not in VALID_FILEPATH_FILTER_MODES:
        normalized_filter_mode = "prefix"
    candidate_entries = collect_note_title_candidates(min_term_length=normalized_min_term_length)
    results = []
    matched_file_count = 0
    scanned_file_count = 0
    scan_errors = []
    scan_error_count = 0
    max_scan_errors = 5

    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]

        for name in filenames:
            if not name.endswith(".md"):
                continue

            file_path = os.path.join(root, name)
            rel_path = os.path.relpath(file_path, VAULT_PATH)

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
                    scan_errors.append(
                        {
                            "file": rel_path,
                            "error_type": type(exc).__name__,
                        }
                    )
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
    template_path = Path(__file__).parent / "templates" / "note_template.md"
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
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

    if not is_safe_path(filepath):
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
            f"Error: Invalid path '{filepath}'.",
            meta=meta,
        )

    path_parts = Path(filepath).parts
    if len(path_parts) > 1:
        top_level_folder = VAULT_PATH / path_parts[0]
        if not top_level_folder.exists():
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
                f"Error: Structural policy prevents creating new root-level folders ('{path_parts[0]}'). Please place the report in an existing folder like '00_Inbox'.",
                meta=meta | {"top_level_folder": path_parts[0]},
            )

    summary = TRACKER.summarize_records(
        days=days,
        include_prompts=include_prompts,
        name_filter=tool_name,
        exclude_names={"get_token_usage_report", "write_token_usage_report_note"},
        since_call_id=since_call_id,
    )
    report = TRACKER.render_markdown_report(summary)

    target_file.parent.mkdir(parents=True, exist_ok=True)
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(report)

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
