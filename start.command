#!/bin/bash
# Double-click this file to start SquishBox on macOS
cd "$(dirname "$0")"
# Kill any existing SquishBox
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
python3 app.py
