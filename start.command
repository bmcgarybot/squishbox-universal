#!/bin/bash
# Double-click to start SquishBox on macOS
# Prevents sleep + auto-restarts on crash
cd "$(dirname "$0")"
# Kill any OTHER SquishBox before we start
kill $(lsof -ti :5555) 2>/dev/null
sleep 2
source venv/bin/activate 2>/dev/null
echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║  Keep this window open!           ║"
echo "  ║  SquishBox runs here for          ║"
echo "  ║  Full Disk Access.                ║"
echo "  ║  Close window = stop SquishBox    ║"
echo "  ╚═══════════════════════════════════╝"
echo ""
while true; do
    caffeinate -i python3 app.py
    CODE=$?
    echo ""
    echo "  ⚠️  SquishBox stopped (exit $CODE). Restarting in 5 seconds..."
    echo "  (Close this window to stop for real)"
    echo ""
    sleep 5
done
