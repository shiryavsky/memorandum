#!/bin/bash
# Setup script for macOS local testing
# Usage: ./setup.sh

set -e

echo "Setting up Memorandum Message Collector..."

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "Python version: $PYTHON_VERSION"

if [ "$(echo "$PYTHON_VERSION < 3.11" | bc -l)" -eq 1 ]; then
    echo "Error: Python 3.11+ required"
    exit 1
fi

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv .venv

# Activate virtual environment
echo "Activating virtual environment..."
source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip

# On Linux, FlagEmbedding pulls torch with bundled CUDA wheels (~1.3 GB).
# Pre-install CPU-only torch from the PyTorch CPU index so the CUDA wheels
# (cudnn, nccl, cusparselt, etc.) are skipped. macOS torch is CPU by default.
if [ "$(uname -s)" = "Linux" ]; then
    echo "Installing CPU-only PyTorch (skipping CUDA bundle)..."
    pip install --index-url https://download.pytorch.org/whl/cpu torch
fi

pip install -r requirements.txt

# Create data directory
echo "Creating data directory..."
mkdir -p data

# Create config.yaml from the example template if it doesn't exist.
# config.example.yaml is the source of truth — keeping a separate inline
# template in this script means the two drift apart on every new key.
if [ ! -f config.yaml ]; then
    if [ -f config.example.yaml ]; then
        echo "Creating config.yaml from config.example.yaml..."
        cp config.example.yaml config.yaml
        echo "Created config.yaml - please edit it with your credentials"
    else
        echo "Warning: config.example.yaml not found, skipping config.yaml bootstrap."
    fi
fi

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit config.yaml with your Mattermost credentials"
echo "  2. Test ingest: ./run_ingest.sh --hours 24"
echo ""
echo "For Linux with systemd (recommended for production):"
echo "  sudo cp systemd/memorandum-collect.service /etc/systemd/system/"
echo "  sudo cp systemd/memorandum-collect.timer /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable --now memorandum-collect.timer"
echo ""
echo "For macOS or non-systemd environments:"
echo "  ./bin/memorandum-sync"
echo ""
