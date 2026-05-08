import json
import re
from difflib import SequenceMatcher


HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


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
