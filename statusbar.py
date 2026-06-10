"""Menu bar status item for Mumbletype."""

import AppKit
from Foundation import NSObject

from config import Config
from mainthread import run_on_main

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

    def openPreferences_(self, sender):
        self._ctrl._open_preferences()

    def resetUsage_(self, sender):
        self._ctrl._config.reset_usage()

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)


class StatusBarController:
    """NSStatusItem with dropdown menu for Mumbletype controls.

    The menu is built once; status/config changes update item titles in place,
    always on the main thread (update_status and config listeners may be
    invoked from any thread).
    """

    def __init__(self, config: Config, hotkey_manager=None):
        global _controller
        _controller = self  # prevent GC

        self._config = config
        self._hotkey_manager = hotkey_manager
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
        self._refresh_titles()
        self._config.add_listener(lambda: run_on_main(self._refresh_titles))

    def update_status(self, state: str):
        labels = {"idle": "Idle", "recording": "Recording...", "transcribing": "Transcribing..."}
        label = labels.get(state, state)
        run_on_main(lambda: self._status_mi.setTitle_(f"Status: {label}"))

    def _build_menu(self):
        menu = AppKit.NSMenu.alloc().init()

        def add_disabled(title):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, None, ""
            )
            item.setEnabled_(False)
            menu.addItem_(item)
            return item

        add_disabled("Mumbletype")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._status_mi = add_disabled("Status: Idle")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        # Model submenu
        self._model_mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Model", None, ""
        )
        model_submenu = AppKit.NSMenu.alloc().init()
        self._model_items = {}
        for model_id, info in Config.MODELS.items():
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                info["label"], "selectModel:", ""
            )
            mi.setTarget_(self._delegate)
            mi.setRepresentedObject_(model_id)
            model_submenu.addItem_(mi)
            self._model_items[model_id] = mi
        self._model_mi.setSubmenu_(model_submenu)
        menu.addItem_(self._model_mi)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._usage_mi = add_disabled("Usage:")

        reset_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Reset Usage Stats…", "resetUsage:", ""
        )
        reset_item.setTarget_(self._delegate)
        menu.addItem_(reset_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        self._hotkey_mi = add_disabled("Hotkey:")

        prefs_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Preferences…", "openPreferences:", ","
        )
        prefs_item.setTarget_(self._delegate)
        menu.addItem_(prefs_item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Mumbletype", "quitApp:", "q"
        )
        quit_item.setTarget_(self._delegate)
        menu.addItem_(quit_item)

        self._status_item.setMenu_(menu)

    def _refresh_titles(self):
        """Update config-derived titles in place. Main thread only."""
        current_model = self._config.get_model()
        model_label = Config.MODELS.get(current_model, {}).get("label", current_model)
        self._model_mi.setTitle_(f"Model: {model_label}")
        for model_id, mi in self._model_items.items():
            mi.setState_(
                AppKit.NSControlStateValueOn
                if model_id == current_model
                else AppKit.NSControlStateValueOff
            )

        usage = self._config.get_usage()
        total_min = usage["total_seconds"] / 60.0
        self._usage_mi.setTitle_(
            f"Usage: {total_min:.1f} min · ${usage['total_cost_usd']:.4f}"
            f" · {usage['session_count']} sessions"
        )

        self._hotkey_mi.setTitle_(f"Hotkey: {self._config.get_hotkey()[2]}")

    def _open_preferences(self):
        from preferences import PreferencesWindowController

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)

        if self._prefs_window is None or self._prefs_window._window is None:
            self._prefs_window = PreferencesWindowController(
                self._config, self._hotkey_manager, self._on_prefs_closed
            )
        self._prefs_window.show()

    def _on_prefs_closed(self):
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
