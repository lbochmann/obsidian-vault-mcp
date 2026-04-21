# obsidian-vault-mcp

Local-first, privacy-aware MCP for Obsidian vaults.

A lightweight Model Context Protocol (MCP) server that lets assistants work against plain
Markdown notes without defaulting to embeddings, vector databases, or whole-document dumps.

This project is intentionally opinionated: inspectable, local-only, token-aware, and small
enough that a reviewer can understand the entire system quickly.

## Why This Exists

Most "AI for personal knowledge" setups drift toward one of two extremes:

* **Heavy infrastructure:** embeddings, vector databases, ingestion jobs, sync logic, and operational overhead.
* **Naive prompting:** dumping whole notes into the model and paying for far more context than a query actually needs.

This project explores a third path:

* keep the source of truth as plain Markdown in an Obsidian vault
* expose a small set of deterministic tools to the model
* retrieve only the smallest useful context first
* apply privacy controls locally before note content leaves the machine
* measure whether the retrieval strategy actually saves tokens

This is not trying to be a generic RAG platform. It is trying to be a practical local document interface for real knowledge work.

## Architecture Overview

```text
Obsidian Vault (Markdown on disk)
        |
        v
obsidian-vault-mcp
        |
        +-- Retrieval tools
        |     - search_vault
        |     - get_vault_structure
        |     - get_note_outline
        |     - read_note_section
        |     - read_note
        |
        +-- Write / workflow tools
        |     - write_note
        |     - archive_note
        |     - find_stale_notes
        |
        +-- Privacy layer
        |     - regex masking
        |     - optional Presidio NLP masking
        |     - configured NLP language
        |     - per-note masking strategy
        |
        +-- Telemetry layer
              - JSONL usage logs
              - token approximation
              - estimated savings
              - Markdown usage reports
        |
        v
Claude Desktop / any MCP-capable client
```

### Retrieval Flow

1. The model searches the vault instead of loading whole files immediately.
2. The server returns structured, bounded results.
3. The model inspects outlines before reading larger notes.
4. The model reads only the relevant section when possible.
5. Privacy controls run locally before content is returned.
6. Telemetry tracks what was returned and estimates savings versus naive full-note reads.

## What The Server Is Optimized For

### Token-Efficient Retrieval

Instead of treating the vault like a bag of full documents, the server encourages staged retrieval:

* `search_vault` searches note contents and filenames and can limit search to a filepath prefix
* `get_note_outline` exposes structure before content
* `read_note_section` reads semantically from heading to heading and supports pagination
* `read_note` remains available as a fallback, not the default

This is effectively "RAG light" built on deterministic Markdown structure rather than embeddings.

### Local Privacy Controls

The privacy model is layered:

* **Regex masking** is the fast path for known sensitive patterns such as tokens, IDs, IPs, or domain-specific secrets.
* **Presidio NLP masking** is the deeper pass for broader personal data such as names, addresses, phone numbers, and financial identifiers.

To avoid damaging technical notes, the default `balanced` mode protects:

* fenced code blocks
* inline code
* raw URLs

from the NLP masking pass while still allowing regex redaction globally.

### Pragmatic Telemetry

Telemetry is optional and local-only. When enabled, it tracks:

* argument tokens
* result tokens
* total tool-call tokens
* estimated saved result tokens versus naive full-note reads
* latency

## Features

* Pure Python retrieval over local Markdown files
* Structured search results instead of whole-document dumps
* Outline-first and section-level reads for large notes
* Optional local telemetry with token estimates and savings reports
* Regex plus optional Presidio-based masking
* Per-note masking strategies via frontmatter
* Search scan-error reporting instead of silent failure
* Path traversal protection on reads and writes
* Smoke tests for the most important retrieval and telemetry paths

## Quick Start

### 1. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your local config files

```bash
cp config.example.json config.json
cp privacy_rules.example.json privacy_rules.json
```

Edit `config.json` and set:

* `vault_path` to your actual Obsidian vault
* `privacy.nlp_language` to `de` or `en`
* telemetry settings if you want local usage tracking

Edit `privacy_rules.json` and tailor the regex rules to your own environment.

### 3. Install the Presidio language model

```bash
./setup_presidio.sh
```

The setup script reads `config.json` if it exists, otherwise `config.example.json`, and installs only the SpaCy model for the configured NLP language.

### 4. Add the server to Claude Desktop

On macOS, edit:

`~/Library/Application Support/Claude/claude_desktop_config.json`

Example:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "/Path/to/your/obsidian-vault-mcp/venv/bin/python",
      "args": [
        "/Path/to/your/obsidian-vault-mcp/server.py"
      ]
    }
  }
}
```

Afterwards, fully quit Claude Desktop and restart it.

### 5. Optional: Use the companion skill

The repo also includes [`obsidian-analyst.md`](obsidian-analyst.md), a companion skill/prompt
file for Claude or other MCP-capable assistants. It is optional, but it helps reinforce the
intended retrieval order (`search_vault` -> `get_note_outline` -> `read_note_section` ->
`read_note`), note-writing discipline, and privacy-warning behavior when working against this
server.

## Configuration

### `config.example.json`

This file documents the expected runtime config shape. Copy it to `config.json` for your local setup.

Key settings:

* `vault_path`: absolute path to your Obsidian vault
* `ignored_folders`: folders excluded from listing and search
* `privacy.nlp_language`: Presidio language, currently `de` or `en`
* `telemetry.enabled`: toggle local JSONL usage tracking

### `privacy_rules.example.json`

This file documents the regex masking layer. Copy it to `privacy_rules.json` and tailor it to your environment.

Use it for:

* internal IDs
* tokens or API keys
* IPs, MAC addresses, URLs, or hostnames
* any domain-specific patterns that general NLP models should not be trusted to catch

### Per-note masking

Each note can declare its own masking strategy in frontmatter:

```yaml
mcp_masking: balanced
```

Available modes:

* `required`: regex masking plus Presidio across the full note
* `balanced`: regex masking globally, but code/URLs are protected from Presidio
* `clear`: no masking; the note is returned as-is

## Telemetry Notes

Telemetry is most useful for comparing strategies and trends, not exact invoicing.

Important caveats:

* `cl100k_base` uses `tiktoken` for a stronger local approximation
* counts are still approximate and will not match provider billing 1:1
* structured JSON results add some overhead, but the staged retrieval flow still tends to save substantial context versus naive whole-note reads

## Repo Structure

```text
.
├── README.md
├── LICENSE
├── requirements.txt
├── server.py
├── telemetry.py
├── setup_presidio.sh
├── config.example.json
├── obsidian-analyst.md
├── privacy_rules.example.json
├── templates/
│   └── note_template.md
└── tests/
    ├── test_server_smoke.py
    └── test_telemetry.py
```

## Smoke Tests

Run the lightweight regression checks with:

```bash
python3 -m unittest discover -s tests -v
```

The current smoke tests cover:

* partial search scan failures without losing valid hits
* telemetry error status on invalid paths
* telemetry log and summary behavior
* configured Presidio NLP language usage

## Example Usage

Try prompts like:

* *"Search my vault for notes on ransomware resilience."*
* *"Search for `hardening` and include a few surrounding lines for each hit."*
* *"Search for `setVariable` only inside `04_Knowledge/Commvault/`."*
* *"Show me the outline of `Linux Kompendium` before reading it."*
* *"Read the section `Praxis / Kommandos` from this note and continue if `truncated` is true."*
* *"Try reading `Praxis` with fuzzy heading matching enabled."*
* *"Create a new technical knowledge note and choose an appropriate `mcp_masking` mode."*
* *"Show me the MCP token usage report for the last 30 days."*
* *"Write the telemetry report into `00_Inbox/MCP Tool Usage Report.md`."*

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
