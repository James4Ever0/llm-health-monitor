#!/usr/bin/env bash
set -euo pipefail

# Change to the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Source the virtual environment
if [ -f "venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source venv/bin/activate
else
    echo "ERROR: venv/bin/activate not found. Run: python3 -m venv venv && pip install -r requirements.txt" >&2
    exit 1
fi

# Ensure config.yaml exists; if not, copy from the sample
if [ ! -f "config.yaml" ]; then
    echo "WARNING: config.yaml not found. Copying from config.yaml.sample (if available)." >&2
    if [ -f "config.yaml.sample" ]; then
        cp config.yaml.sample config.yaml
    else
        echo "ERROR: No config.yaml or config.yaml.sample found." >&2
        exit 1
    fi
fi

# Run the app
exec python app.py "$@"
