#!/usr/bin/env bash
# Installs tracker.py as a per-user background service on macOS.
# It starts at login, restarts if it stops, and keeps writing to the same tracker.db.
# Run it from anywhere:  bash deploy/install_service.sh
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
# Use the real interpreter, not a pyenv shim, so it also works outside your shell
PYTHON="$(python3 -c 'import sys; print(sys.executable)')"
LABEL="com.t212tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/t212-tracker.log"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# Fill in the paths in the template
sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__PROJECT__|$PROJECT|g" \
    -e "s|__LOG__|$LOG|g" \
    "$PROJECT/deploy/$LABEL.plist.template" > "$PLIST"

# Replace any previously installed version
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed: $PLIST"
echo "Python:    $PYTHON"
echo "Project:   $PROJECT"
echo "Log:       $LOG"
echo
echo "Check it is running:  launchctl list | grep t212tracker"
echo "Watch the log:        tail -f \"$LOG\""
echo "Stop it:              launchctl bootout gui/\$(id -u) \"$PLIST\""
