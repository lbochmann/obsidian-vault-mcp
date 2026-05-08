import os
from fnmatch import fnmatch
from pathlib import Path


VALID_FILEPATH_FILTER_MODES = {"prefix", "substring", "glob"}


def normalize_markdown_filepath(filepath: str) -> str:
    """Ensure note-oriented tools consistently operate on Markdown file paths."""
    if filepath.endswith(".md"):
        return filepath
    return f"{filepath}.md"


def resolve_vault_target(vault_path: Path, filepath: str) -> Path:
    """Resolve a vault-relative path to an absolute local filesystem path."""
    return (vault_path / filepath).resolve()


def resolve_markdown_target(vault_path: Path, filepath: str) -> tuple[str, Path]:
    """Normalize a note path and resolve it inside the configured vault."""
    normalized_filepath = normalize_markdown_filepath(filepath)
    return normalized_filepath, resolve_vault_target(vault_path, normalized_filepath)


def is_safe_path(vault_path: Path, requested_path: str) -> bool:
    """Prevents Path Traversal outside the vault."""
    vault_root = vault_path.resolve()
    target_path = (vault_root / requested_path).resolve()
    return target_path.is_relative_to(vault_root)


def validate_markdown_write_target(vault_path: Path, filepath: str, target_file: Path) -> str | None:
    """Return an error string when a Markdown write would violate vault policy."""
    if not is_safe_path(vault_path, filepath):
        return f"Error: Invalid path '{filepath}'."

    path_parts = Path(filepath).parts
    if len(path_parts) > 1:
        top_level_folder = vault_path / path_parts[0]
        if not top_level_folder.exists():
            return (
                f"Error: Structural policy prevents creating new root-level folders ('{path_parts[0]}'). "
                "Please place the note in an existing folder like '00_Inbox'. Use 'list_notes' or "
                "'search_vault' to find the correct directory."
            )

    if not target_file.resolve().is_relative_to(vault_path.resolve()):
        return f"Error: Invalid path '{filepath}'."

    return None


def read_text_file(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def iter_markdown_files(vault_path: Path, ignored_folders: list[str]) -> list[tuple[str, Path]]:
    files = []
    for root, dirs, filenames in os.walk(vault_path):
        dirs[:] = [d for d in dirs if d not in ignored_folders and not d.startswith('.')]
        for name in filenames:
            if name.endswith(".md"):
                file_path = Path(root) / name
                rel_path = os.path.relpath(file_path, vault_path)
                files.append((rel_path, file_path))
    return files


def normalize_filepath_filter(filepath_filter: str) -> str:
    return filepath_filter.strip().replace("\\", "/").lstrip("/")


def normalize_filepath_filter_mode(filepath_filter_mode: str) -> str:
    normalized_filter_mode = filepath_filter_mode.strip().lower()
    if normalized_filter_mode not in VALID_FILEPATH_FILTER_MODES:
        return "prefix"
    return normalized_filter_mode


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
