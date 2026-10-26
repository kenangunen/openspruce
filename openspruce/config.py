"""Paths shared by the app and the login script."""

import shutil
from pathlib import Path

APP_NAME = "OpenSpruce"
APP_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
APP_DIR.mkdir(parents=True, exist_ok=True)

USER_FILE = APP_DIR / "user.json"          # e-mail + Roborock token (UserData)
CACHE_FILE = APP_DIR / "device_cache.pkl"  # home data / network info cache
PREFS_FILE = APP_DIR / "prefs.json"        # selected device, rooms, repeat count
LOG_FILE = APP_DIR / "app.log"
MAP_FILE = APP_DIR / "map.png"

# One-time migration from the pre-release name ("RoborockMenu").
_OLD_DIR = Path.home() / "Library" / "Application Support" / "RoborockMenu"
if _OLD_DIR.is_dir() and not USER_FILE.exists():
    for name in ("user.json", "prefs.json"):
        if (_OLD_DIR / name).exists():
            shutil.copy2(_OLD_DIR / name, APP_DIR / name)
