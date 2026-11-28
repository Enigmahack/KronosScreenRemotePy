#!/usr/bin/env bash
set -euo pipefail

# Move to the folder containing this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

# Activate the virtual environment for this script's execution
source "$VENV_DIR/bin/activate"

if [ -f "requirements.txt" ]; then
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
fi

exec python ./main.py
