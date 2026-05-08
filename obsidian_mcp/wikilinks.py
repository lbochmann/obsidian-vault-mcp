import re
from pathlib import Path

from .markdown import normalize_search_text
from .privacy import INLINE_CODE_PATTERN, apply_masking
from .vault import iter_markdown_files, normalize_markdown_filepath, resolve_vault_target


WIKILINK_PATTERN = re.compile(r"\[\[[^\]]+\]\]")
WIKILINK_CAPTURE_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")
FENCE_TOGGLE_PATTERN = re.compile(r"^\s*(```|~~~)")


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


def collect_note_title_candidates(vault_path: Path, ignored_folders: list[str], min_term_length: int) -> list[dict]:
    candidates_by_key: dict[str, dict] = {}

    for rel_path, _ in iter_markdown_files(vault_path, ignored_folders):
        title = Path(rel_path).stem.strip()
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


def resolve_backlink_aliases(vault_path: Path, filepath_or_title: str) -> set[str]:
    normalized_input = filepath_or_title.strip().replace("\\", "/").lstrip("/")
    if not normalized_input:
        return set()

    path_like = normalized_input.endswith(".md") or "/" in normalized_input
    if path_like:
        return wikilink_aliases_for_path(normalized_input)

    aliases = {normalize_wikilink_target(normalized_input)}
    candidate_path = normalize_markdown_filepath(normalized_input)
    candidate_file = resolve_vault_target(vault_path, candidate_path)
    if candidate_file.exists():
        aliases.update(wikilink_aliases_for_path(candidate_path))
    return aliases


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


def find_wikilink_backlinks(
    *,
    vault_path: Path,
    ignored_folders: list[str],
    target_aliases: set[str],
    filepath_filter: str,
    filepath_filter_mode: str,
    filepath_matches_filter,
) -> dict:
    results = []
    matched_file_count = 0
    scanned_file_count = 0
    scan_errors = []
    scan_error_count = 0
    max_scan_errors = 5

    for rel_path, file_path in iter_markdown_files(vault_path, ignored_folders):
        if not filepath_matches_filter(rel_path, filepath_filter, filepath_filter_mode):
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

    return {
        "matched_file_count": matched_file_count,
        "scanned_file_count": scanned_file_count,
        "result_count": len(results),
        "scan_error_count": scan_error_count,
        "scan_errors_truncated": scan_error_count > len(scan_errors),
        "scan_errors": scan_errors,
        "results": results,
    }
