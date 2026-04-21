# Obsidian MCP Server for Claude Desktop

A lightweight, local Model Context Protocol (MCP) server for Obsidian vaults.

This project is intentionally opinionated: local-first, inspectable, privacy-aware, and small enough that the architecture itself can serve as an Applied AI showcase.

## Why Does This Project Exist?

Most "AI for personal knowledge" setups drift toward one of two extremes:

* **Heavy infrastructure:** embeddings, vector databases, sync layers, ingestion jobs, and operational overhead.
* **Naive prompting:** dumping large documents into the model and paying for far more context than is actually useful.

This project exists to explore a third path:

* keep the source of truth as plain Markdown in an Obsidian vault
* expose only a small set of deterministic tools to the model
* optimize for privacy, retrieval quality, and token efficiency
* make the system understandable enough that a human can reason about every layer

In short: this is not trying to be a generic RAG platform. It is trying to be a practical, local document interface for real knowledge work.

## Architecture At A Glance

```text
Obsidian Vault (Markdown on disk)
        |
        v
Obsidian MCP Server (this project)
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
        |     - per-note masking strategy
        |
        +-- Telemetry layer
              - JSONL tool usage logs
              - token approximation
              - savings estimates
              - Markdown usage reports
        |
        v
Claude Desktop / MCP-compatible client
```

### Design Flow

1. Claude asks for the smallest useful context first.
2. The server returns structured, bounded results instead of whole-document dumps.
3. Privacy controls run locally before note content leaves the machine.
4. Telemetry measures how much context was actually returned and how much likely got saved.

## What The Server Is Optimized For

### 1. Token-Efficient Retrieval

Instead of loading full notes by default, the server encourages a staged retrieval flow:

* `search_vault` searches note contents and filenames, with optional surrounding line context
* `get_note_outline` reveals structure before content
* `read_note_section` reads semantically from heading to heading
* large sections can be paged with `offset_lines` and `max_lines`
* `read_note` remains available as a fallback, not the default path

This is basically "RAG light" built on top of deterministic Markdown structure rather than embeddings.

### 2. Local Privacy Controls

The privacy model is intentionally layered:

* **Regex masking** runs globally and is best for domain-specific secrets such as internal IDs, API keys, or known sensitive patterns.
* **Presidio NLP masking** is reserved for broader personal data such as names, addresses, phone numbers, or financial identifiers.

To avoid damaging technical notes, the default `balanced` mode protects:

* fenced code blocks
* inline code
* raw URLs

from the Presidio NLP pass while still allowing regex redaction to run globally.

### 3. Mixed-Vault Support

Not every note should be treated the same way. A technical Bash note, a customer conversation, and an investment note have very different privacy and retrieval needs.

Each note can declare its own strategy via frontmatter:

```yaml
mcp_masking: balanced
```

Available modes:

* `required`: regex masking plus Presidio across the full note
* `balanced`: regex masking globally, but code/URLs are protected from Presidio
* `clear`: regex masking only, no Presidio

Default behavior:

* if a note has no `mcp_masking` field, the server falls back to `balanced`
* the bundled MCP template defaults to `mcp_masking: balanced`

### 4. Local Telemetry Instead of Guesswork

The server can write local JSONL events for tool and prompt calls and estimate:

* argument tokens
* result tokens
* total tool-call tokens
* estimated "saved" tokens versus naive full-note reads
* latency and vault-size context

Claude can also read these reports back through MCP or write them into the vault as Markdown notes.

## Features

* **No Vector Database Required:** pure Python retrieval over local Markdown files.
* **Semantic Token Optimization:** outlines, section reads, and chunked retrieval reduce unnecessary context.
* **Optional Token Telemetry:** local JSONL logging with approximate token counts and savings.
* **Built-in Telemetry Reports:** usage reports can be rendered directly in Claude or written back into Obsidian.
* **Hybrid PII Masking:** regex plus optional Presidio-based NLP masking.
* **Code-Aware Privacy Filtering:** code fences, inline code, and URLs are protected from NLP over-masking in `balanced` mode.
* **Per-Note Masking Strategies:** each note can opt into `required`, `balanced`, or `clear`.
* **Path Traversal Protection:** reads and writes are constrained to the configured vault.
* **Inbox Zero Workflow:** `archive_note` supports safe movement from inbox-style folders into archive storage.
* **Formatting Prompt:** the server ships with a Markdown template optimized for structured, retrieval-friendly notes.

## Writing For Retrieval

This server works best when notes are written as small semantic blocks instead of large monolithic sections.

Recommended style:

* use clear H2 blocks for major topics
* split large sections into focused H3 subsections
* prefer multiple short code examples over one huge command dump
* if one subsection grows beyond roughly 20-30 lines, split it further

That is why the bundled MCP template nudges Claude toward smaller subheadings and cleaner thematic separation.

## Templates And Vault Structure

There are two template concepts in this setup:

* `templates/note_template.md` inside this repository is the MCP server's own authoring template, exposed through the `note_format` prompt
* your Obsidian vault may also contain normal Markdown templates such as `_Templates/Knowledge-Note.md` or Templater-based files

These do not technically conflict.

The practical question is only whether vault templates should be visible to MCP tools:

* if you want Claude to inspect or reuse vault templates, keep them accessible
* if you want to hide them from search and listing, add `_Templates` to `ignored_folders` in `config.json`

## Setup

### Why You Should Use A Virtual Environment

The most common installation issues on macOS come from global Python package installs, especially with newer Python/Homebrew setups.

Commands like `python3 pip install -r requirements.txt` are invalid because Python tries to execute a file named `pip`. Even the correct form, `python3 -m pip install ...`, may run into `PEP-668` restrictions on system-managed environments.

The clean solution is to run the server in its own virtual environment.

### Installation

1. Open a terminal and go to the project directory:

   ```bash
   cd /path/to/your/obsidian-mcp
   ```

2. Create a virtual environment:

   ```bash
   python3 -m venv venv
   ```

3. Activate it:

   ```bash
   source venv/bin/activate
   ```

4. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

5. Initialize Presidio / SpaCy models:

   ```bash
   ./setup_presidio.sh
   ```

6. Configure `config.json`:

   * set `"vault_path"` to your actual Obsidian vault
   * adjust `ignored_folders` as needed
   * customize `privacy_rules.json` for your own regex redaction patterns

### Optional: Enable Telemetry

Telemetry is disabled by default and can be toggled in `config.json`:

```json
"telemetry": {
  "enabled": true,
  "log_path": ".mcp-telemetry/tool_usage.jsonl",
  "tokenizer": "cl100k_base",
  "hash_filepaths": true,
  "include_vault_stats": true
}
```

Notes:

* `tokenizer: cl100k_base` uses `tiktoken` for a stronger local approximation
* counts are still approximate and are not guaranteed to match Claude billing 1:1
* telemetry is most useful for comparing strategies and trends, not for exact invoicing

## Claude Desktop Configuration

On macOS, edit:

`~/Library/Application Support/Claude/claude_desktop_config.json`

Add a server entry that points to the Python interpreter inside your virtual environment:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "/Path/to/your/obsidian-mcp/venv/bin/python",
      "args": [
        "/Path/to/your/obsidian-mcp/server.py"
      ]
    }
  }
}
```

Replace `/Path/to/your/` with the real absolute path to your project.

Afterwards, fully quit Claude Desktop and restart it.

## Example Usage

Try prompts like:

* *"Search my vault for notes on ransomware resilience."*
* *"Search for `hardening` and include a few surrounding lines for each hit."*
* *"Show me the outline of `Linux Kompendium` before reading it."*
* *"Read the section `Praxis / Kommandos` from this note."*
* *"Continue that section with the next chunk."*
* *"Create a new technical knowledge note and choose an appropriate `mcp_masking` mode."*
* *"Show me the MCP token usage report for the last 30 days."*
* *"Write the telemetry report into `00_Inbox/MCP Tool Usage Report.md`."*

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
