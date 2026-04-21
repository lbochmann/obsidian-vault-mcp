#!/bin/bash
set -euo pipefail

CONFIG_FILE="${1:-config.json}"
if [ ! -f "$CONFIG_FILE" ]; then
  CONFIG_FILE="config.example.json"
fi
NLP_LANGUAGE="$(python -c 'import json, sys; from pathlib import Path; config_path = Path(sys.argv[1]); config = json.loads(config_path.read_text(encoding="utf-8")); print(str(config.get("privacy", {}).get("nlp_language", "de")).strip().lower())' "$CONFIG_FILE")"

case "$NLP_LANGUAGE" in
  de)
    SPACY_MODEL="de_core_news_lg"
    ;;
  en)
    SPACY_MODEL="en_core_web_lg"
    ;;
  *)
    echo "Unsupported privacy.nlp_language '$NLP_LANGUAGE' in $CONFIG_FILE. Use 'de' or 'en'."
    exit 1
    ;;
esac

echo "==============================================="
echo " Setting up Microsoft Presidio for Hybrid PII"
echo "==============================================="
echo "Configured NLP language: $NLP_LANGUAGE"
echo "SpaCy model to install: $SPACY_MODEL"

echo "1. Installing Python dependencies..."
pip install -r requirements.txt

echo "2. Downloading SpaCy model for configured language..."
python -m spacy download "$SPACY_MODEL"

echo "==============================================="
echo " Setup complete! Your Hybrid Search is ready."
echo "==============================================="
