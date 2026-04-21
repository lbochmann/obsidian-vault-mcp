#!/bin/bash

echo "==============================================="
echo " Setting up Microsoft Presidio for Hybrid PII"
echo "==============================================="

echo "1. Installing Python dependencies..."
pip install -r requirements.txt

echo "2. Downloading SpaCy Models (DE and EN)..."
# We download both German (DACH region) and English (Fallback)
python -m spacy download de_core_news_lg
python -m spacy download en_core_web_lg

echo "==============================================="
echo " Setup complete! Your Hybrid Search is ready."
echo "==============================================="
