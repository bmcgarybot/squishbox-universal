#!/bin/bash
# Double-click to start SquishBox on macOS
# Prevents sleep + auto-restarts on crash
cd "$(dirname "$0")"
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
    # Kill any lingering SquishBox processes
    pkill -f "python.*app.py" 2>/dev/null
    sleep 2
    # Wait for port 5555 to be free
    while lsof -i :5555 >/dev/null 2>&1; do
        echo "  ⏳ Waiting for port 5555 to free up..."
        sleep 2
    done
    caffeinate -i python3 app.py
    echo ""
    echo "  ⚠️  SquishBox stopped. Restarting in 3 seconds..."
    echo "  (Close this window to stop for real)"
    echo ""
    sleep 3
done
