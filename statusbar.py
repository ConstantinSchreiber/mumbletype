"""Menu bar status item for Mumbletype."""

import AppKit
from Foundation import NSObject

from config import Config

# Strong ref to prevent GC
_controller = None


class _MenuDelegate(NSObject):
    """Handles menu item actions."""

    def initWithController_(self, ctrl):
        self = self.init()
        self._ctrl = ctrl
        return self

    def selectModel_(self, sender):
        model_id = sender.representedObject()
        self._ctrl._config.set_model(model_id)
        self._ctrl.refresh()

    def openPreferences_(self, sender):
        self._ctrl._open_preferences()

    def resetUsage_(self, sender):
        self._ctrl._config.reset_usage()
        self._ctrl.refresh()

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)


class StatusBarController:
    """NSStatusItem with dropdown menu for Mumbletype controls."""

    def __init__(self, config: Config):
        global _controller
        _controller = self  # prevent GC

        self._config = config
        self._status = "Idle"
        self._prefs_window = None
        self._delegate = _MenuDelegate.alloc().initWithController_(self)

        self._status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )

        # Menu bar icon
        button = self._status_item.button()
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "mic.fill", "Mumbletype"
        )
        if img is None:
            # Fallback for older macOS
            button.setTitle_("W")
        else:
            img.setTemplate_(True)
            button.setImage_(img)

        self._build_menu()
        self._config.add_listener(self.refresh)

    def update_status(self, state: str):
        labels = {"idle": "Idle", "recording": "Recording...", "transcribing": "Transcribing..."}
        self._status = labels.get(state, state)
        self.refresh()

    def refresh(self):
        self._build_menu()

    def _build_menu(self):
        menu = AppKit.NSMenu.alloc().init()

        # Title
        title_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Mumbletype", None, ""
        )
        title_item.setEnabled_(False)
        menu.addItem_(title_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Status
        status_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Status: {self._status}", None, ""
        )
        status_item.setEnabled_(False)
        menu.addItem_(status_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Model submenu
        current_model = self._config.get_model()
        model_label = Config.MODELS.get(current_model, {}).get("label", current_model)
        model_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Model: {model_label}", None, ""
        )
        model_submenu = AppKit.NSMenu.alloc().init()
        for model_id, info in Config.MODELS.items():
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                info["label"], "selectModel:", ""
            )
            mi.setTarget_(self._delegate)
            mi.setRepresentedObject_(model_id)
            if model_id == current_model:
                mi.setState_(AppKit.NSControlStateValueOn)
            model_submenu.addItem_(mi)
        model_item.setSubmenu_(model_submenu)
        menu.addItem_(model_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Usage
        usage = self._config.get_usage()
        total_min = usage["total_seconds"] / 60.0
        total_cost = usage["total_cost_usd"]
        sessions = usage["session_count"]
        usage_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Usage: {total_min:.1f} min \u00b7 ${total_cost:.4f} \u00b7 {sessions} sessions",
            None, "",
        )
        usage_item.setEnabled_(False)
        menu.addItem_(usage_item)

        reset_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Reset Usage Stats\u2026", "resetUsage:", ""
        )
        reset_item.setTarget_(self._delegate)
        menu.addItem_(reset_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Hotkey info
        hk_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Hotkey: Ctrl+D", None, ""
        )
        hk_item.setEnabled_(False)
        menu.addItem_(hk_item)

        # Preferences
        prefs_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Preferences\u2026", "openPreferences:", ","
        )
        prefs_item.setTarget_(self._delegate)
        menu.addItem_(prefs_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Quit
        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Mumbletype", "quitApp:", "q"
        )
        quit_item.setTarget_(self._delegate)
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

    def _open_preferences(self):
        from preferences import PreferencesWindowController

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)

        if self._prefs_window is None or self._prefs_window._window is None:
            self._prefs_window = PreferencesWindowController(self._config, self._on_prefs_closed)
        self._prefs_window.show()

    def _on_prefs_closed(self):
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
