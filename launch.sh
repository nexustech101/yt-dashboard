#!/usr/bin/env bash
# Run with `./launch.sh` (Linux) or double-click (macOS: see
# launch.command instead). First run sets up a virtual environment
# and installs dependencies; every run after that just starts the app.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "First run - setting up, this takes a minute..."
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install .
fi

./.venv/bin/streamlit run app.py