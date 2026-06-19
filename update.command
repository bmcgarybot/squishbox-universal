#!/bin/bash
# Double-click this file to update & restart SquishBox on macOS
cd "$(dirname "$0")"
echo "📦 Pulling latest code..."
git pull
echo "🔄 Stopping background service (if running)..."
launchctl unload "$HOME/Library/LaunchAgents/com.squishbox.server.plist" 2>/dev/null
# Kill any existing SquishBox python processes
pkill -f "python.*app.py" 2>/dev/null
sleep 1
echo "🚀 Starting SquishBox in Terminal (Full Disk Access)..."
source venv/bin/activate 2>/dev/null
echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║  Keep this window open!           ║"
echo "  ║  SquishBox runs here for          ║"
echo "  ║  Full Disk Access.                ║"
echo "  ║  Close window = stop SquishBox    ║"
echo "  ╚═══════════════════════════════════╝"
echo ""
python3 app.py
