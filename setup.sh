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

# Create config.yaml from template if it doesn't exist
if [ ! -f config.yaml ]; then
    echo "Creating config.yaml..."
    cat > config.yaml << 'EOF'
# Memorandum Message Collector Configuration
sqlite_path: "data/messages.db"
chroma_path: "data/chroma"

# Telegram - TODO: Implement connector when ready
# telegram:
#   enabled: false
#   api_id: 123456
#   api_hash: "abc123..."
#   session_name: "data/telegram"

# Mattermost - Active implementation
mattermost:
  enabled: true
  url: "https://mattermost.yourcompany.com"
  token: "your-personal-access-token"

filters:
  skip_sources: []
  skip_senders:
    - "webhook-bot"
    - "github-bot"
  skip_channels:
    - "random"
    - "off-topic"
  only_channels: []
  skip_patterns:
    - "^Reminder:"
    - "joined the channel"

schedule_minutes: 15
EOF
    echo "Created config.yaml - please edit it with your credentials"
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
