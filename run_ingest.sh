#!/bin/bash
# Quick test script for one-off ingestion
# Usage: ./run_ingest.sh [--hours HOURS] [--debug] [--force]
#
# This script now uses the same lock mechanism as the systemd service.
# It will fail immediately if another instance is running.

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Default: fetch last 24 hours (for manual runs, use larger window)
HOURS=${HOURS:-24}
DEBUG=""
FORCE=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --hours)
            HOURS="$2"
            shift 2
            ;;
        --debug)
            DEBUG="--debug"
            shift
            ;;
        --force)
            FORCE="--force"
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--hours HOURS] [--debug] [--force]"
            echo ""
            echo "Options:"
            echo "  --hours HOURS   Hours back to fetch (default: 24)"
            echo "  --debug         Enable debug logging"
            echo "  --force         Ignore saved channel state"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "Running ingest for last $HOURS hours..."
[ -n "$DEBUG" ] && echo "Debug mode enabled"
[ -n "$FORCE" ] && echo "Force mode: ignoring saved channel state"

# Create virtual environment if needed
FRESH_VENV=false
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    FRESH_VENV=true
fi

# Install dependencies if needed (always install if venv is fresh, or if packages are missing)
if [ "$FRESH_VENV" = true ]; then
    echo "Installing dependencies..."
    .venv/bin/pip install -r requirements.txt -q
elif ! .venv/bin/python -c "import yaml" 2>/dev/null; then
    echo "Installing dependencies..."
    .venv/bin/pip install -r requirements.txt -q
fi

# Prevent HuggingFace model checks if model is cached
if [ -n "${HOME:-}" ] && [ -d "$HOME/.cache/huggingface/hub/models--BAAI"* ] 2>/dev/null; then
    export HF_HUB_OFFLINE=1
fi

# Use the main sync script which handles locking
if [ -x "./bin/memorandum-sync" ]; then
    exec ./bin/memorandum-sync --hours "$HOURS" --config config.yaml $FORCE
else
    echo "Warning: bin/memorandum-sync not found or not executable"
    echo "Running ingest directly (without lock protection)..."
    .venv/bin/python -m pipeline --hours "$HOURS" --config config.yaml $DEBUG $FORCE
fi
