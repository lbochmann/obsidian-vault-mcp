import os
import shutil
import json
import re
from datetime import datetime
from pathlib import Path
from mcp.server.fastmcp import FastMCP

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

def apply_deep_masking(text: str) -> str:
    """Applies Hybrid Masking: First Regex (Fast-Path), then Presidio NLP (Deep Scanning)."""
    # 1. First Pass: Custom Domain Regex Masking (Fast Path for internal IDs, Project names)
    masked_text = apply_masking(text)
    
    # 2. Second Pass: Deep PII Scan via Presidio (if available)
    analyzer, anonymizer = get_presidio_engines()
    if analyzer and anonymizer:
        try:
            # Attempt parsing in 'de', catching regional specific PII and general NLP.
            results = analyzer.analyze(text=masked_text, language='de')
            if results:
                anonymized_result = anonymizer.anonymize(text=masked_text, analyzer_results=results)
                masked_text = anonymized_result.text
        except Exception as e:
            print(f"Warning: Presidio deep masking failed: {e}")
            
    return masked_text

@mcp.tool()
def list_notes(directory: str = "") -> str:
    """Lists all Markdown files in the Vault or a specific subfolder."""
    target_dir = (VAULT_PATH / directory).resolve()
    
    if not is_safe_path(directory) or not target_dir.exists():
        return f"Error: Path '{directory}' does not exist or is invalid."

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
        return "No Markdown files found."
        
    return "Found notes:\n" + "\n".join(f"- {f}" for f in sorted(files))

@mcp.tool()
def get_vault_structure(max_depth: int = 2) -> str:
    """Returns the folder structure (directories only) of the Vault as a tree. Extremely useful to understand the categorization system without loading thousands of files."""
    
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
        return "No directories found or depth limit reached."
        
    return f"Vault Folder Structure (Max Depth {max_depth}):\n" + "\n".join(tree)

@mcp.tool()
def read_note(filepath: str) -> str:
    """Reads the content of a specific Markdown file."""
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    
    if not is_safe_path(filepath) or not target_file.exists():
        return f"Error: File '{filepath}' not found."
        
    with open(target_file, "r", encoding="utf-8") as f:
        content = f.read()
        
    # Apply deep hybrid masking before data leaves the local server
    masked_content = apply_deep_masking(content)
    
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
        
    return masked_content

@mcp.tool()
def search_vault(query: str) -> str:
    """Simple full-text search across all notes in the vault. Avoids expensive Embedding/RAG pipelines."""
    query = query.lower()
    results = []
    
    for root, dirs, filenames in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in IGNORED_FOLDERS and not d.startswith('.')]
        
        for name in filenames:
            if name.endswith(".md"):
                file_path = os.path.join(root, name)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        
                    for i, line in enumerate(lines):
                        if query in line.lower():
                            rel_path = os.path.relpath(file_path, VAULT_PATH)
                            # Return the line and surrounding context
                            snippet = line.strip()
                            # Apply masking to snippets
                            snippet_masked = apply_masking(snippet)
                            results.append(f"[{rel_path}] Line {i+1}: ...{snippet_masked}...")
                except Exception:
                    pass # Ignore read errors for binary or weird files
                    
    if not results:
        return f"No matches found for '{query}'."
        
    # Limit results to save tokens
    max_results = 20
    output = "\n".join(results[:max_results])
    if len(results) > max_results:
        output += f"\n... and {len(results) - max_results} more matches."
        
    return f"Search results for '{query}':\n\n{output}"

@mcp.tool()
def get_note_outline(filepath: str) -> str:
    """Returns a semantic outline (table of contents) of a Markdown file by extracting its headers. Useful for token optimization."""
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    
    if not is_safe_path(filepath) or not target_file.exists():
        return f"Error: File '{filepath}' not found."
        
    with open(target_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    outline = []
    for line in lines:
        if line.startswith("#"):
            outline.append(line.strip())
            
    if not outline:
        return "No headers found in this file."
        
    return "Note Outline:\n" + "\n".join(outline)

@mcp.tool()
def read_note_section(filepath: str, heading: str) -> str:
    """Reads a specific section of a Markdown file under a given heading until the next equal/higher heading. Great to save tokens."""
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    
    if not is_safe_path(filepath) or not target_file.exists():
        return f"Error: File '{filepath}' not found."
        
    with open(target_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    section_lines = []
    in_section = False
    
    heading_stripped = heading.strip()
    target_level = 0
    for char in heading_stripped:
        if char == '#':
            target_level += 1
        else:
            break
            
    if target_level == 0:
        return "Error: Provided heading does not start with '#'."

    for line in lines:
        line_stripped = line.strip()
        
        if not in_section:
            if line_stripped.startswith(heading_stripped):
                in_section = True
                section_lines.append(line)
        else:
            # Drop out of section if we hit a new header of equal or higher priority
            if line_stripped.startswith("#"):
                current_level = 0
                for char in line_stripped:
                    if char == '#':
                        current_level += 1
                    else:
                        break
                
                if current_level > 0 and current_level <= target_level:
                    break
                    
            section_lines.append(line)
            
    if not section_lines:
        return f"Error: Heading '{heading}' not found in file."
        
    content = "".join(section_lines)
    
    # We apply deep masking just like in the full read
    masked_content = apply_deep_masking(content)
    
    analyzer, _ = get_presidio_engines()
    if not analyzer:
        warning_block = (
            "> [!WARNING] SYSTEM NOTE TO LLM\n"
            "> The Deep PII NLP Filter (Presidio) is currently unavailable or failed to load. "
            "> Only basic Regex domain masking was applied. "
            "> YOU MUST explicitly warn the user about this in your response.\n\n"
        )
        masked_content = warning_block + masked_content
        
    return masked_content

@mcp.tool()
def write_note(filepath: str, content: str) -> str:
    """Creates or overwrites a Markdown note. Please use the 'note_format' prompt beforehand for the correct layout."""
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    target_file = (VAULT_PATH / filepath).resolve()
    
    if not is_safe_path(filepath):
        return f"Error: Invalid path '{filepath}'."
        
    # Structural Security Check: Prevent the LLM from hallucinating new root-level folders.
    path_parts = Path(filepath).parts
    if len(path_parts) > 1:
        top_level_folder = VAULT_PATH / path_parts[0]
        if not top_level_folder.exists():
            return f"Error: Structural policy prevents creating new root-level folders ('{path_parts[0]}'). Please place the note in an existing folder like '00_Inbox'. Use 'list_notes' or 'search_vault' to find the correct directory."
            
    # Create folder structure if it doesn't exist (only allows sub-folders inside existing root folders now)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(content)
        
    return f"Note successfully written: {filepath}"

@mcp.tool()
def archive_note(filepath: str) -> str:
    """Moves a processed note from the Inbox or Clippings folder to the Archive folder to keep the workspace clean."""
    if not filepath.endswith(".md"):
        filepath += ".md"
        
    source_file = (VAULT_PATH / filepath).resolve()
    
    if not is_safe_path(filepath) or not source_file.exists():
        return f"Error: File '{filepath}' not found or invalid path."
        
    # Security Check: Only allow archiving from Inbox or Clippings
    rel_path = os.path.relpath(source_file, VAULT_PATH)
    if not (rel_path.startswith("00_Inbox") or rel_path.startswith("Clippings")):
        return f"Error: Archiving is only permitted for the '00_Inbox' or 'Clippings' folders to prevent accidental moves. Path was '{rel_path}'."
        
    archive_dir = (VAULT_PATH / "08_Archive" / "Processed_Clippings").resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    target_file = archive_dir / source_file.name
    
    # Avoid overwriting if a file with the same name already exists in the archive
    counter = 1
    while target_file.exists():
        target_file = archive_dir / f"{source_file.stem}_{counter}.md"
        counter += 1
        
    shutil.move(str(source_file), str(target_file))
    
    return f"Success! Note moved from '{rel_path}' to '{os.path.relpath(target_file, VAULT_PATH)}'."

@mcp.tool()
def find_stale_notes(days_old: int = 90) -> str:
    """Finds notes that haven't been updated in a given number of days to help review stale content."""
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
        return f"Good job! No notes older than {days_old} days found."
        
    # Sort by oldest first
    stale_files.sort(key=lambda x: x[1], reverse=True)
    
    output = f"Found {len(stale_files)} notes older than {days_old} days:\n" 
    output += "\n".join(f"- [{f[0]}] (Last updated: {f[2]}, {f[1]} days ago)" for f in stale_files[:30])
    if len(stale_files) > 30:
        output += f"\n... and {len(stale_files) - 30} more."
    return output

@mcp.prompt()
def note_format() -> str:
    """Fetch this formatting template before writing a new note."""
    template_path = Path(__file__).parent / "templates" / "note_template.md"
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = f.read()
            return (
                f"Please format all new notes exactly according to this template:\n\n{template}\n\n"
                f"CRITICAL INSTRUCTION: You MUST fill out the 'sources: []' YAML tag and the '## Sources' section at the bottom with explicit origin URLs or references. "
                f"This is required so the system can verify information for depreciation later.\n"
                f"CRITICAL INSTRUCTION 2: When updating an existing note via write_note, you MUST NOT delete the old information entirely. Instead, summarize the old state and add it to the '## Changelog / History' section, along with the date of the change, so historical knowledge is preserved.\n"
                f"CRITICAL INSTRUCTION 3: You MUST structure the note semantically using H2 ('##') blocks for entirely new sections. This strict hierarchy allows the LLM to navigate the note via 'read_note_section' later without consuming massive token limits.\n\n"
                f"Current Date for Frontmatter: {datetime.now().strftime('%Y-%m-%d')}"
            )
    except Exception:
        return "Write the note in standard Markdown, starting with a YAML frontmatter. You MUST include original source URLs as references."

if __name__ == "__main__":
    mcp.run()
