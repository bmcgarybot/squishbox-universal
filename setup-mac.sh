#!/bin/bash
# SquishBox macOS Auto-Start Setup
# Run this once to make SquishBox start automatically on login

SQUISHBOX_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$SQUISHBOX_DIR/venv/bin/python3"
PLIST_NAME="com.squishbox.server"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

echo ""
echo "  🗜️  SquishBox macOS Auto-Start Setup"
echo ""

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "  ⚠️  ffmpeg not found. Installing via Homebrew..."
    if ! command -v brew &>/dev/null; then
        echo "  ❌ Homebrew not installed. Run this first:"
        echo '     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        exit 1
    fi
    brew install ffmpeg
fi

# Verify VideoToolbox support
if ffmpeg -encoders 2>/dev/null | grep -q hevc_videotoolbox; then
    echo "  ✅ ffmpeg has VideoToolbox (hardware HEVC encoding)"
else
    echo "  ⚠️  ffmpeg found but no VideoToolbox. Reinstalling..."
    brew reinstall ffmpeg
fi

# Find ffmpeg path for launchd (it can't see Homebrew PATH)
FFMPEG_DIR="$(dirname "$(which ffmpeg)")"
echo "  → ffmpeg location: $FFMPEG_DIR"

# Check venv exists
if [ ! -f "$VENV_PYTHON" ]; then
    echo "  ⚠️  No venv found. Creating one..."
    python3 -m venv "$SQUISHBOX_DIR/venv"
    "$SQUISHBOX_DIR/venv/bin/pip" install flask
fi

# Create LaunchAgent directory if needed
mkdir -p "$HOME/Library/LaunchAgents"

# Write the plist (with PATH so ffmpeg is found)
cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PYTHON</string>
        <string>$SQUISHBOX_DIR/app.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SQUISHBOX_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$FFMPEG_DIR:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SQUISHBOX_DIR/squishbox.log</string>
    <key>StandardErrorPath</key>
    <string>$SQUISHBOX_DIR/squishbox.log</string>
</dict>
</plist>
PLIST

# Load the service
launchctl unload "$PLIST_PATH" 2>/dev/null
launchctl load "$PLIST_PATH"

echo ""
echo "  ✅ SquishBox installed as login service"
echo "  → Auto-starts on login"
echo "  → Auto-restarts if it crashes"
echo "  → Dashboard: http://localhost:5555"
echo "  → Log file: $SQUISHBOX_DIR/squishbox.log"
echo ""
