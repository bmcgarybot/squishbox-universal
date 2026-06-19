#!/bin/bash
# Double-click this file to update & restart SquishBox on macOS
cd "$(dirname "$0")"
echo "📦 Pulling latest code..."
git pull
echo "🔄 Restarting SquishBox..."
launchctl kickstart -k "gui/$(id -u)/com.squishbox.server" 2>/dev/null && echo "✅ Restarted!" || echo "⚠️ Service not found — start it with: ./setup-mac.sh"
echo ""
echo "Done! You can close this window."
read -p ""
