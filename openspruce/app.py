"""OpenSpruce: macOS menu bar app for Roborock vacuums (built for the Saros 10).

Clicking the menu bar icon opens a popover with an HTML interface (ui/index.html)
rendered by WKWebView. The page talks to Python through a script message
handler named "rr"; Python pushes state into the page via ``window.render``.

Run:  python -m openspruce.app
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSMinYEdge,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSViewController,
)
from Foundation import NSURL, NSMakeRect, NSMakeSize, NSObject
from PyObjCTools import AppHelper
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

from .backend import RUNNING_STATES, Backend
from .config import LOG_FILE, MAP_FILE
from . import labels as L

HERE = Path(__file__).parent
ICON = HERE / "icon.png"
UI_DIR = HERE / "ui"
UI_FILE = UI_DIR / "index.html"
WIDTH, HEIGHT = 400, 660

WKScriptMessageHandler = objc.protocolNamed("WKScriptMessageHandler")
WKNavigationDelegate = objc.protocolNamed("WKNavigationDelegate")

_LOGGER = logging.getLogger(__name__)


def notify(title: str, text: str) -> None:
    title = title.replace('"', "'")
    text = text.replace('"', "'")
    subprocess.Popen(["osascript", "-e", f'display notification "{text}" with title "{title}"'])


class Bridge(NSObject, protocols=[WKScriptMessageHandler]):
    """Receives messages posted by the page: rr.postMessage(JSON string)."""

    def initWithApp_(self, app):
        self = objc.super(Bridge, self).init()
        self.app = app
        return self

    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        try:
            data = json.loads(str(message.body()))
        except Exception:  # noqa: BLE001
            return
        self.app.on_message(data)


class Controller(NSObject):
    def init(self):
        self = objc.super(Controller, self).init()
        self.page_ready = False
        self.last_sig = None
        self.prev_state = None
        self.prev_error = 0
        self.closed_at = 0.0
        self.backend = Backend(on_change=lambda: AppHelper.callAfter(self.push))

        # --- status bar item
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        img = NSImage.alloc().initWithContentsOfFile_(str(ICON))
        img.setSize_(NSMakeSize(18, 18))
        img.setTemplate_(True)
        button = self.item.button()
        button.setImage_(img)
        button.setImagePosition_(2)  # NSImageLeft (icon, then optional battery text)
        button.setTarget_(self)
        button.setAction_("toggle:")

        # --- web view in a popover
        config = WKWebViewConfiguration.alloc().init()
        ucc = WKUserContentController.alloc().init()
        self.bridge = Bridge.alloc().initWithApp_(self)
        ucc.addScriptMessageHandler_name_(self.bridge, "rr")
        config.setUserContentController_(ucc)
        self.web = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, WIDTH, HEIGHT), config)
        self.web.setValue_forKey_(False, "drawsBackground")
        self.web.loadFileURL_allowingReadAccessToURL_(
            NSURL.fileURLWithPath_(str(UI_FILE)), NSURL.fileURLWithPath_(str(UI_DIR))
        )
        vc = NSViewController.alloc().init()
        vc.setView_(self.web)
        self.popover = NSPopover.alloc().init()
        self.popover.setContentSize_(NSMakeSize(WIDTH, HEIGHT))
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setAnimates_(True)
        self.popover.setContentViewController_(vc)
        self.popover.setDelegate_(self)

        self.backend.start()
        return self

    # ------------------------------------------------------------ popover
    def popoverDidClose_(self, _notification):
        self.closed_at = time.monotonic()

    def toggle_(self, _sender):
        # A click on the icon first closes the transient popover; don't reopen it.
        if time.monotonic() - self.closed_at < 0.3:
            return
        if self.popover.isShown():
            self.popover.performClose_(None)
            return
        button = self.item.button()
        self.popover.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, NSMinYEdge)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.js("window.onOpen && window.onOpen()")
        self.backend.run("refresh_fast")

    @objc.python_method
    def js(self, code: str) -> None:
        if self.page_ready:
            self.web.evaluateJavaScript_completionHandler_(code, None)

    # ------------------------------------------------------------- state
    @objc.python_method
    def push(self) -> None:
        snap = self.backend.snapshot
        sig = json.dumps(snap, sort_keys=True, default=str)
        if sig == self.last_sig:
            return
        self.last_sig = sig
        self.notify_changes(snap)
        state = snap.get("state_key")
        batt = snap.get("battery")
        title = f" {batt}%" if (state in RUNNING_STATES and batt is not None) else ""
        self.item.button().setTitle_(title)
        self.js(f"window.render({sig})")

    @objc.python_method
    def notify_changes(self, s: dict) -> None:
        state = s.get("state_key")
        if self.prev_state in RUNNING_STATES and state in ("returning_home", "charging", "docking", "idle"):
            area = f" · {s['area_m2']} m²" if s.get("area_m2") else ""
            notify(s.get("name", "Roborock"), f"Temizlik bitti{area}")
        err = s.get("error_code") or s.get("dock_error_code") or 0
        if err and err != self.prev_error:
            notify(s.get("name", "Roborock"), "Hata: " + L.pretty(s.get("error_key") or s.get("dock_error_key"), L.ERRORS))
        self.prev_state, self.prev_error = state, err

    # ---------------------------------------------------------- messages
    @objc.python_method
    def on_message(self, data: dict) -> None:
        action = data.get("action")
        args = data.get("args") or []
        if action == "ready":
            self.page_ready = True
            self.last_sig = None
            self.push()
        elif action == "quit":
            NSApplication.sharedApplication().terminate_(None)
        elif action == "close":
            self.popover.performClose_(None)
        elif action == "open_map":
            if MAP_FILE.exists():
                subprocess.Popen(["open", str(MAP_FILE)])
        elif action == "open_log":
            subprocess.Popen(["open", str(LOG_FILE)])
        elif action == "set_pref":
            key, value = args
            self.backend.set_pref(key, value)
            self.backend.republish()
        elif action and hasattr(self.backend, f"a_{action}"):
            self.backend.run(action, *args)
        else:
            _LOGGER.warning("Unknown UI action: %s", action)


def main() -> None:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2)
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    controller = Controller.alloc().init()
    app.setDelegate_(controller)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
