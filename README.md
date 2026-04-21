# Obsidian MCP Server for Claude Desktop

A lightweight, local Model Context Protocol (MCP) Server for Obsidian vaults. This server was developed primarily for **Pragmatism and Privacy**.

It allows LLM clients like the Claude Desktop App to securely access personal notes, search through them, and create new, cleanly structured notes—without needing external vector databases or complex ML pipelines.

## Features

- **No Vector Database Required:** A blazing fast, pure Python full-text search (`search_vault`) saves you configuration headaches and hosting costs.
- **Semantic Token Optimization:** Instead of loading massive files that drain context limits, Claude can scan a note's structure (`get_note_outline`) and selectively read specific headings (`read_note_section`) to save up to 95% API tokens.
- **Hybrid PII Masking:** Features a custom Two-Tier security filter. First, a fast Regex layer (`privacy_rules.json`) strips domain-specific secrets. Second, a deep NLP scanner via **Microsoft Presidio** strips global PII (Names, IBANs, Locations) before data reaches the LLM. 
- **In-Band System Telemetry:** If the NLP layer fails to load, the server automatically injects a hidden warning prompt into the context, turning the LLM into an intelligent active error-handler that warns the user.
- **Path Traversal Protection:** The LLM has limited write access constrained strictly to the defined Vault folder. Underlying system files remain protected.
- **Inbox Zero (Safe Archiving):** The `archive_note` tool allows the LLM to automatically move processed clippings from your `00_Inbox` or `Clippings` folders to `08_Archive/Processed_Clippings`. No dangerous delete permissions, just a clean, reversible workflow.
- **Formatting Prompts:** The server provides Claude with a fixed, strictly Markdown-hierarchical template (`templates/note_template.md`), aligning perfectly with Zettelkasten / Second Brain principles.

---

## Installation & Setup (macOS / Linux)

### Important: Why you MUST use Virtual Environments
The most common source of installation issues is installing packages globally on macOS (especially with Python >= 3.11 or Homebrew). 

> **Troubleshooting Syntax:** Commands like `python3 pip install -r requirements.txt` are syntactically incorrect and will fail with `can't open file '.../pip': No such file` because Python attempts to execute a file named "pip".

Even the correct command (`python3 -m pip install...`) is often blocked on modern Macs by `PEP-668` ("externally-managed-environment") to protect the OS.

**The Solution: Setup the server in an isolated Virtual Environment.**

### Step-by-step Installation

1. **Open your terminal and navigate to the project folder:**
   ```bash
   cd /path/to/your/obsidian-mcp
   ```

2. **Create the Virtual Environment (venv):**
   ```bash
   python3 -m venv venv
   ```

3. **Activate the Environment:**
   ```bash
   source venv/bin/activate
   ```

4. **Install the dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

5. **Initialize Hybrid NLP (Microsoft Presidio):**
   Run the setup script to download the SpaCy language models for advanced PII scanning.
   ```bash
   ./setup_presidio.sh
   ```

5. **Configure the Server:**
   Open `config.json` and adjust the `"vault_path"` to point to your actual Obsidian Vault folder. Customize your masking rules in `privacy_rules.json` if desired.

---

## Claude Desktop Configuration

Once the server is installed, you must tell Claude Desktop how to launch it.
We edit the Claude Desktop Configuration block for this. On macOS, this file is located at:
`~/Library/Application Support/Claude/claude_desktop_config.json`

Insert the following block into your configuration. **Important:** As the command, we explicitly pass the path to the Python interpreter inside our generated `venv`. This ensures the script runs safely isolated.

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
*(Note: Replace `/Path/to/your/` with the actual absolute path to your project folder)*

**Afterwards, quit the Claude Desktop App entirely (CMD+Q) and restart it.** 
You should now see the small tool icon (hammer) in Claude's text input bar, confirming the server is actively connected.

---

## How to use the server

Try using prompts like these in Claude Desktop:
* *"Search my vault for notes on Ransomware resilience."*
* *"What files exist in the IT-Knowledge folder?"*
* *"Read the file `mcp_test.md`."*
* *"Summarize what I've read and create a new note for it in the vault. Make sure to fetch the format rules for notes beforehand!"*

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details. 
You are free to use, copy, modify, and distribute this software for any purpose, including commercial use.
