"""Preferences window for Mumbletype."""

import AppKit
import sounddevice as sd
from Foundation import NSObject

from config import Config


class _WindowDelegate(NSObject):
    """Handles window lifecycle events."""

    def initWithController_(self, ctrl):
        self = self.init()
        self._ctrl = ctrl
        return self

    def windowWillClose_(self, notification):
        self._ctrl._on_close()


class _ButtonTarget(NSObject):
    """Routes button actions back to the controller."""

    def initWithController_(self, ctrl):
        self = self.init()
        self._ctrl = ctrl
        return self

    def save_(self, sender):
        self._ctrl._save()

    def cancel_(self, sender):
        self._ctrl._cancel()

    def toggleKeyVisibility_(self, sender):
        self._ctrl._toggle_key_visibility()

    def validateKey_(self, sender):
        self._ctrl._validate_key()


class PreferencesWindowController:
    """NSWindow-based preferences panel."""

    _WIDTH = 480
    _HEIGHT = 440

    def __init__(self, config: Config, on_close_callback=None):
        self._config = config
        self._on_close_callback = on_close_callback
        self._window = None
        self._key_field = None
        self._key_secure_field = None
        self._key_visible = False
        self._model_popup = None
        self._device_popup = None
        self._delegate = _WindowDelegate.alloc().initWithController_(self)
        self._target = _ButtonTarget.alloc().initWithController_(self)
        self._validation_label = None
        self._devices = []

    def show(self):
        if self._window is not None:
            self._window.makeKeyAndOrderFront_(None)
            return
        self._build()
        self._window.makeKeyAndOrderFront_(None)

    def _build(self):
        w, h = self._WIDTH, self._HEIGHT
        frame = ((200, 200), (w, h))
        mask = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, mask, AppKit.NSBackingStoreBuffered, False
        )
        self._window.setTitle_("Mumbletype Preferences")
        self._window.setDelegate_(self._delegate)
        self._window.setLevel_(AppKit.NSFloatingWindowLevel)

        content = self._window.contentView()
        y = h - 40  # current y position, top-down

        # ── API Key section ──────────────────────────────────────────────
        y = self._add_section_label(content, "OpenAI API Key", y)

        # Secure field (default)
        self._key_secure_field = AppKit.NSSecureTextField.alloc().initWithFrame_(
            ((20, y - 28), (w - 160, 24))
        )
        self._key_secure_field.setStringValue_(self._config.get_api_key())
        self._key_secure_field.setPlaceholderString_("sk-...")
        content.addSubview_(self._key_secure_field)

        # Plain text field (hidden initially)
        self._key_field = AppKit.NSTextField.alloc().initWithFrame_(
            ((20, y - 28), (w - 160, 24))
        )
        self._key_field.setStringValue_(self._config.get_api_key())
        self._key_field.setPlaceholderString_("sk-...")
        self._key_field.setHidden_(True)
        content.addSubview_(self._key_field)

        # Show/Hide button
        show_btn = AppKit.NSButton.alloc().initWithFrame_(((w - 130, y - 28), (50, 24)))
        show_btn.setTitle_("Show")
        show_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        show_btn.setTarget_(self._target)
        show_btn.setAction_("toggleKeyVisibility:")
        content.addSubview_(show_btn)
        self._show_btn = show_btn

        # Validate button
        validate_btn = AppKit.NSButton.alloc().initWithFrame_(((w - 75, y - 28), (55, 24)))
        validate_btn.setTitle_("Test")
        validate_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        validate_btn.setTarget_(self._target)
        validate_btn.setAction_("validateKey:")
        content.addSubview_(validate_btn)

        y -= 36

        # Validation result label
        self._validation_label = AppKit.NSTextField.labelWithString_("")
        self._validation_label.setFrame_(((20, y - 18), (w - 40, 16)))
        self._validation_label.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self._validation_label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        content.addSubview_(self._validation_label)
        y -= 30

        # ── Model section ────────────────────────────────────────────────
        y = self._add_section_label(content, "Transcription Model", y)

        self._model_popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((20, y - 28), (w - 40, 24)), False
        )
        current_model = self._config.get_model()
        for model_id, info in Config.MODELS.items():
            rate = info["rate_per_min"]
            self._model_popup.addItemWithTitle_(f"{info['label']}  (${rate:.3f}/min)")
            self._model_popup.lastItem().setRepresentedObject_(model_id)
            if model_id == current_model:
                self._model_popup.selectItem_(self._model_popup.lastItem())
        content.addSubview_(self._model_popup)
        y -= 44

        # ── Audio Device section ─────────────────────────────────────────
        y = self._add_section_label(content, "Audio Input Device", y)

        self._device_popup = AppKit.NSPopUpButton.alloc().initWithFrame_pullsDown_(
            ((20, y - 28), (w - 40, 24)), False
        )
        self._populate_devices()
        content.addSubview_(self._device_popup)
        y -= 44

        # ── Usage section ────────────────────────────────────────────────
        y = self._add_section_label(content, "Usage Statistics", y)

        usage = self._config.get_usage()
        total_min = usage["total_seconds"] / 60.0
        total_cost = usage["total_cost_usd"]
        sessions = usage["session_count"]
        usage_text = (
            f"Total audio: {total_min:.1f} min   |   "
            f"Est. cost: ${total_cost:.4f}   |   "
            f"Sessions: {sessions}"
        )
        self._usage_label = AppKit.NSTextField.labelWithString_(usage_text)
        self._usage_label.setFrame_(((20, y - 20), (w - 40, 16)))
        self._usage_label.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        content.addSubview_(self._usage_label)
        y -= 40

        # ── Hotkey info ──────────────────────────────────────────────────
        y = self._add_section_label(content, "Record Hotkey", y)

        hk_label = AppKit.NSTextField.labelWithString_("Ctrl + D")
        hk_label.setFrame_(((20, y - 20), (w - 40, 16)))
        hk_label.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        content.addSubview_(hk_label)
        y -= 40

        # ── Bottom bar ───────────────────────────────────────────────────
        cancel_btn = AppKit.NSButton.alloc().initWithFrame_(((w - 170, 12), (75, 30)))
        cancel_btn.setTitle_("Cancel")
        cancel_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        cancel_btn.setTarget_(self._target)
        cancel_btn.setAction_("cancel:")
        cancel_btn.setKeyEquivalent_("\x1b")  # Escape key
        content.addSubview_(cancel_btn)

        save_btn = AppKit.NSButton.alloc().initWithFrame_(((w - 85, 12), (75, 30)))
        save_btn.setTitle_("Save")
        save_btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        save_btn.setTarget_(self._target)
        save_btn.setAction_("save:")
        save_btn.setKeyEquivalent_("\r")  # Enter key
        content.addSubview_(save_btn)

    def _add_section_label(self, view, text, y):
        label = AppKit.NSTextField.labelWithString_(text)
        label.setFrame_(((20, y - 20), (self._WIDTH - 40, 16)))
        label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
        view.addSubview_(label)
        return y - 24

    def _populate_devices(self):
        self._device_popup.removeAllItems()
        self._device_popup.addItemWithTitle_("System Default")
        self._device_popup.lastItem().setRepresentedObject_(None)

        current_device = self._config.get_audio_device()
        self._devices = []

        try:
            devices = sd.query_devices()
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0:
                    self._devices.append((i, dev["name"]))
                    self._device_popup.addItemWithTitle_(dev["name"])
                    self._device_popup.lastItem().setRepresentedObject_(i)
                    if i == current_device:
                        self._device_popup.selectItem_(self._device_popup.lastItem())
        except Exception:
            pass

    def _toggle_key_visibility(self):
        self._key_visible = not self._key_visible
        if self._key_visible:
            # Copy value from secure to plain, show plain
            self._key_field.setStringValue_(self._key_secure_field.stringValue())
            self._key_secure_field.setHidden_(True)
            self._key_field.setHidden_(False)
            self._show_btn.setTitle_("Hide")
        else:
            # Copy value from plain to secure, show secure
            self._key_secure_field.setStringValue_(self._key_field.stringValue())
            self._key_field.setHidden_(True)
            self._key_secure_field.setHidden_(False)
            self._show_btn.setTitle_("Show")

    def _validate_key(self):
        key = self._get_current_key()
        if not key:
            self._validation_label.setStringValue_("No API key entered")
            self._validation_label.setTextColor_(AppKit.NSColor.systemRedColor())
            return
        try:
            from openai import OpenAI
            test_client = OpenAI(api_key=key)
            test_client.models.list()
            self._validation_label.setStringValue_("Valid API key")
            self._validation_label.setTextColor_(AppKit.NSColor.systemGreenColor())
        except Exception as e:
            self._validation_label.setStringValue_(f"Invalid: {e}")
            self._validation_label.setTextColor_(AppKit.NSColor.systemRedColor())

    def _get_current_key(self) -> str:
        if self._key_visible:
            return self._key_field.stringValue()
        return self._key_secure_field.stringValue()

    def _save(self):
        # API key
        new_key = self._get_current_key()
        if new_key and new_key != self._config.get_api_key():
            self._config.set_api_key(new_key)

        # Model
        selected = self._model_popup.selectedItem()
        if selected:
            model_id = selected.representedObject()
            if model_id:
                self._config.set_model(model_id)

        # Audio device
        dev_selected = self._device_popup.selectedItem()
        if dev_selected:
            self._config.set_audio_device(dev_selected.representedObject())

        self._window.close()

    def _cancel(self):
        self._window.close()

    def _on_close(self):
        self._window = None
        if self._on_close_callback:
            self._on_close_callback()
