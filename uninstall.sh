#!/bin/bash
# Stops OpenSpruce and removes the login item.
# Credentials stay in ~/Library/Application Support/OpenSpruce (delete it manually if you like).
LABEL="com.openspruce.app"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
pkill -f "openspruce.app" 2>/dev/null || true
echo "OpenSpruce uninstalled."
