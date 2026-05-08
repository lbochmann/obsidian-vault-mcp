import re

from .markdown import extract_frontmatter


PROTECTED_SEGMENT_TOKEN = "__MCP_PROTECTED_SEGMENT_"
FENCED_CODE_BLOCK_PATTERN = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?\n\2(?=\n|$)", re.DOTALL)
INLINE_CODE_PATTERN = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}]+")
VALID_MASKING_MODES = {"required", "balanced", "clear"}

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
NLP_LANGUAGE = "de"
PRESIDIO_MODEL = "de_core_news_lg"
privacy_rules = []


def configure_privacy(*, nlp_language: str, presidio_model: str, rules: list[dict]) -> None:
    global NLP_LANGUAGE, PRESIDIO_MODEL, privacy_rules, ANALYZER_ENGINE, ANONYMIZER_ENGINE
    NLP_LANGUAGE = nlp_language
    PRESIDIO_MODEL = presidio_model
    privacy_rules = rules
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


def get_masking_mode(text: str) -> str:
    mode = extract_frontmatter(text).get("mcp_masking", "balanced").strip().strip("'\"").lower()
    if mode in VALID_MASKING_MODES:
        return mode
    return "balanced"


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
