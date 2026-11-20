#!/bin/bash

# path project
PROJECT_DIR="/home/fauzi/crawler-rebuild"
VENV_DIR="$PROJECT_DIR/venv"
LOG_DIR="$PROJECT_DIR/logs"

cd "$PROJECT_DIR"

# aktifkan venv
source "$VENV_DIR/bin/activate"

# jalankan crawler
python3 crawler_google.py >> "$LOG_DIR/$(date +'%Y-%m-%d')-crawler.log" 2>&1
