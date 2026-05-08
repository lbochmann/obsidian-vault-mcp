import json
from pathlib import Path


SUPPORTED_PRESIDIO_LANGUAGES = {
    "de": "de_core_news_lg",
    "en": "en_core_web_lg",
}


def load_json_file(base_dir: Path, preferred_name: str, fallback_name: str) -> tuple[dict, Path]:
    """Load a local JSON file, falling back to the example file in fresh clones."""
    preferred_path = base_dir / preferred_name
    fallback_path = base_dir / fallback_name

    if preferred_path.exists():
        with open(preferred_path, "r", encoding="utf-8") as f:
            return json.load(f), preferred_path

    with open(fallback_path, "r", encoding="utf-8") as f:
        return json.load(f), fallback_path


def load_runtime_config(base_dir: Path) -> dict:
    """Load server config and derive runtime paths/settings."""
    config, config_path = load_json_file(base_dir, "config.json", "config.example.json")
    vault_path = (base_dir / config.get("vault_path", "./test_vault")).resolve()
    ignored_folders = config.get("ignored_folders", [".obsidian", ".git", ".trash"])

    privacy_config = config.get("privacy", {})
    nlp_language = str(privacy_config.get("nlp_language", "de")).strip().lower()
    if nlp_language not in SUPPORTED_PRESIDIO_LANGUAGES:
        nlp_language = "de"
    presidio_model = SUPPORTED_PRESIDIO_LANGUAGES[nlp_language]

    privacy_data, privacy_path = load_json_file(base_dir, "privacy_rules.json", "privacy_rules.example.json")
    privacy_rules = privacy_data.get("rules", [])

    return {
        "config": config,
        "config_path": config_path,
        "vault_path": vault_path,
        "ignored_folders": ignored_folders,
        "nlp_language": nlp_language,
        "presidio_model": presidio_model,
        "privacy_data": privacy_data,
        "privacy_path": privacy_path,
        "privacy_rules": privacy_rules,
    }
