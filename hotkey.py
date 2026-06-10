"""Global record hotkey via Carbon RegisterEventHotKey (quickmachotkey).

Unlike a CGEventTap-based listener (pynput), the registration lives in the
window server: macOS cannot silently disable it when a callback is slow, an
exception cannot kill it, it needs no Accessibility or Input Monitoring
permission, and the bound chord is consumed system-wide (it never reaches the
focused app). The handler fires on the main thread via the NSApplication
event pump.
"""

import logging

import AppKit
from quickmachotkey import quickHotKey
from quickmachotkey.constants import cmdKey, controlKey, kVK_ANSI_V, optionKey, shiftKey

from mainthread import run_on_main

log = logging.getLogger(__name__)

ESCAPE_KEY_CODE = 53

# Cocoa (NSEvent) flag → Carbon mask → display symbol, in macOS display order.
_FLAG_MAP = (
    (AppKit.NSEventModifierFlagControl, controlKey, "⌃"),
    (AppKit.NSEventModifierFlagOption, optionKey, "⌥"),
    (AppKit.NSEventModifierFlagShift, shiftKey, "⇧"),
    (AppKit.NSEventModifierFlagCommand, cmdKey, "⌘"),
)

# Labels for non-character keys (NSEvent keyCode == Carbon virtual key).
_SPECIAL_KEY_LABELS = {
    36: "↩", 48: "⇥", 49: "Space", 51: "⌫", 117: "⌦",
    123: "←", 124: "→", 125: "↓", 126: "↑",
    115: "↖", 119: "↘", 116: "⇞", 121: "⇟",
    122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6",
    98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
}


def carbon_mask_from_cocoa(ns_flags: int) -> int:
    flags = ns_flags & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
    return sum(carbon for cocoa, carbon, _ in _FLAG_MAP if flags & cocoa)


def display_for(carbon_mods: int, key_label: str) -> str:
    return "".join(sym for _, carbon, sym in _FLAG_MAP if carbon_mods & carbon) + key_label


def key_label_for_event(event) -> str:
    label = _SPECIAL_KEY_LABELS.get(event.keyCode())
    if label:
        return label
    chars = event.charactersIgnoringModifiers() or ""
    return chars.upper() if chars.strip() else f"key{event.keyCode()}"


def is_forbidden(virtual_key: int, carbon_mods: int) -> bool:
    """Exactly Cmd+V: a registered chord eats even synthetic events, so this
    would swallow the app's own paste."""
    return virtual_key == kVK_ANSI_V and carbon_mods == cmdKey


class _ConfigBridge:
    """quickmachotkey Configurator backed by Config. Persistence (including the
    display string) is done by Config.set_hotkey, so save here is a no-op."""

    def __init__(self, config):
        self._config = config

    def loadConfiguration(self, fully_qualified_name):
        vk, mods, _ = self._config.get_hotkey()
        return (vk, mods)

    def saveConfiguration(self, fully_qualified_name, virtual_key, modifier_mask):
        pass


class HotkeyManager:
    """Registers the global record hotkey and reconfigures it on config change.

    All Carbon calls happen on the main thread; construct on the main thread.
    """

    def __init__(self, config, on_fire):
        self._config = config
        self._paused = False
        vk, mods, display = config.get_hotkey()
        self._current = (vk, mods)

        @quickHotKey(virtualKey=vk, modifierMask=mods, configurator=_ConfigBridge(config))
        def _handler() -> None:
            try:
                on_fire()
            except Exception:
                # Unlike pynput, a raise here cannot kill the hotkey — but log it.
                log.exception("hotkey handler failed")

        self._handler = _handler
        log.info("hotkey registered: %s", display)
        config.add_listener(lambda: run_on_main(self._apply_config))

    def _apply_config(self):
        vk, mods, display = self._config.get_hotkey()
        if (vk, mods) == self._current:
            return
        self._current = (vk, mods)
        self._handler.configure(virtualKey=vk, modifierMask=mods)
        if self._paused:
            self._handler.unregister()  # configure() re-registers; stay paused
        log.info("hotkey reconfigured: %s", display)

    def pause(self):
        """Temporarily unregister, e.g. while capturing a new shortcut — a
        registered chord is consumed system-wide, so the capture field would
        never see it. Main thread only."""
        if not self._paused:
            self._paused = True
            self._handler.unregister()

    def resume(self):
        if self._paused:
            self._paused = False
            self._handler.register()
