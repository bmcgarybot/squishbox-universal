#!/bin/bash
# Double-click to update + start SquishBox on macOS
cd "$(dirname "$0")"
echo "📦 Pulling latest code..."
git pull
echo "🔄 Stopping background service (if running)..."
launchctl unload "$HOME/Library/LaunchAgents/com.squishbox.server.plist" 2>/dev/null
pkill -f "python.*app.py" 2>/dev/null
sleep 1
source venv/bin/activate 2>/dev/null
echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║  Keep this window open!           ║"
echo "  ║  SquishBox runs here for          ║"
echo "  ║  Full Disk Access.                ║"
echo "  ║  Close window = stop SquishBox    ║"
echo "  ╚═══════════════════════════════════╝"
echo ""
# caffeinate -i keeps Mac awake while this runs
while true; do
    caffeinate -i python3 app.py
    echo ""
    echo "  ⚠️  SquishBox stopped. Restarting in 3 seconds..."
    echo "  (Close this window to stop for real)"
    echo ""
    sleep 3
done
