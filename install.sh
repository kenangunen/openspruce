#!/bin/bash
# OpenSpruce installer
#  1) finds Python 3.11+ (installs it with Homebrew if missing)
#  2) creates .venv and installs dependencies
#  3) signs in to your Roborock account (e-mail code)
#  4) registers a LaunchAgent so the app starts at login, then starts it
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.openspruce.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SUPPORT="$HOME/Library/Application Support/OpenSpruce"
DOMAIN="gui/$(id -u)"

PY=""
for c in python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Python 3.11+ not found, installing python@3.12 with Homebrew..."
    brew install python@3.12
    PY="$(brew --prefix)/bin/python3.12"
  else
    echo "Python 3.11 or newer is required: https://www.python.org/downloads/macos/"
    exit 1
  fi
fi
echo "Python: $PY ($("$PY" --version))"

[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "Dependencies installed."

# Remove the pre-release LaunchAgent if present
OLD="com.roborockmenu.app"
launchctl bootout "$DOMAIN/$OLD" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$OLD.plist"

# Importing config creates the support folder and migrates pre-release credentials
.venv/bin/python -c "import openspruce.config"
if [ ! -f "$SUPPORT/user.json" ] || [ "$1" = "--relogin" ]; then
  .venv/bin/python -m openspruce.login
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>-m</string>
    <string>openspruce.app</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$SUPPORT/stderr.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

# Stop any running copy (bootout is asynchronous, so wait until it is gone)
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
for i in $(seq 1 20); do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
pkill -f "roborock_menu.app" 2>/dev/null || true
pkill -f "openspruce.app" 2>/dev/null || true
if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
  launchctl kickstart -k "$DOMAIN/$LABEL"
fi

echo
echo "Done! The OpenSpruce icon will appear in the menu bar in a few seconds."
echo "Uninstall with: ./uninstall.sh"
