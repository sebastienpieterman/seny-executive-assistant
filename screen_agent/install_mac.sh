#!/bin/bash
# Seny Screen Agent — Mac Installation Script
# Run this from the repo root directory: bash screen_agent/install_mac.sh

set -e  # Exit on any error

REPO_ROOT="$(pwd)"
PLIST_LABEL="com.seny.screenagent"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/SenyScreenAgent"
PYTHON_PATH="$(which python3)"

echo ""
echo "=== Seny Screen Agent — Mac Setup ==="
echo ""

# Check we're in the right directory
if [ ! -f "screen_agent/agent.py" ]; then
    echo "ERROR: Run this script from the repo root directory."
    echo "Example: bash screen_agent/install_mac.sh"
    exit 1
fi

# Check .env exists
if [ ! -f "screen_agent/.env" ]; then
    cp screen_agent/.env.example screen_agent/.env
    echo "Created screen_agent/.env from template."
    echo ""
    echo "IMPORTANT: Edit screen_agent/.env and set your SCREEN_AGENT_KEY before continuing."
    echo "Get your key from: Seny Settings → General → Screen Agent"
    echo ""
    echo "Once you've added your key, run this script again."
    exit 0
fi

# Check SCREEN_AGENT_KEY is set
if grep -q "your_key_here" screen_agent/.env; then
    echo "ERROR: You haven't set your SCREEN_AGENT_KEY in screen_agent/.env"
    echo "Get your key from: Seny Settings → General → Screen Agent"
    exit 1
fi

# Install Python dependencies
echo "Installing Python dependencies..."
$PYTHON_PATH -m pip install -r screen_agent/requirements-mac.txt --quiet
echo "Dependencies installed."
echo ""

# Create log directory
mkdir -p "$LOG_DIR"

# Write LaunchAgent plist
echo "Setting up auto-start..."
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_PATH}</string>
        <string>${REPO_ROOT}/screen_agent/agent.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${REPO_ROOT}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/agent.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/agent-error.log</string>

    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
EOF

# Unload any existing version before loading
launchctl unload "$PLIST_PATH" 2>/dev/null || true

# Load and start the agent
launchctl load "$PLIST_PATH"

echo ""
echo "=== Done! ==="
echo ""
echo "The Seny Screen Agent is now running and will auto-start on every login."
echo "Look for the eye icon (👁) in your Mac menu bar."
echo ""
echo "To pause the agent: click 👁 in the menu bar → Pause"
echo "To stop the agent: click 👁 → Quit"
echo "To uninstall: launchctl unload $PLIST_PATH && rm $PLIST_PATH"
echo ""
echo "Logs: $LOG_DIR/agent.log"
