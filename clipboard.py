"""Clipboard-based text insertion: snapshot → write → Cmd-V → guarded restore."""

import logging
import threading
import time

import AppKit
import Quartz

from mainthread import run_on_main

log = logging.getLogger(__name__)

# Generous restore delay is safe: the changeCount guard means a user copy in
# the meantime always wins, and slow apps (Electron, remote desktops) get time
# to process the synthetic Cmd-V before the pasteboard reverts.
_RESTORE_DELAY = 1.0

_KEY_V = 0x09  # kVK_ANSI_V


def copy_text(text: str):
    """Plain clipboard copy — no paste, no restore. Main thread only."""
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)


def paste_text(text: str):
    """Paste text at the cursor via the clipboard + synthetic Cmd-V.

    Callable from any thread. The previous clipboard contents — all types,
    including images and rich text — are restored afterwards unless the user
    copied something in between.
    """
    pb = AppKit.NSPasteboard.generalPasteboard()
    saved = _snapshot(pb)
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
    change_count = pb.changeCount()
    _post_cmd_v()

    def restore_later():
        time.sleep(_RESTORE_DELAY)
        run_on_main(lambda: _restore(pb, saved, change_count))

    threading.Thread(target=restore_later, name="clipboard-restore", daemon=True).start()


def _snapshot(pb) -> list:
    """Deep-copy all pasteboard items. An NSPasteboardItem belongs to one
    pasteboard forever, so fresh copies are required to write them back."""
    items = []
    for item in pb.pasteboardItems() or []:
        copy = AppKit.NSPasteboardItem.alloc().init()
        copied_any = False
        for t in item.types() or []:
            data = item.dataForType_(t)  # nil for unfulfilled promised types
            if data is not None:
                copy.setData_forType_(data, t)
                copied_any = True
        if copied_any:
            items.append(copy)
    return items


def _restore(pb, items, expected_change_count):
    if pb.changeCount() != expected_change_count:
        return  # the user copied something since the paste — theirs wins
    pb.clearContents()
    if items:
        pb.writeObjects_(items)


def _post_cmd_v():
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    key_down = Quartz.CGEventCreateKeyboardEvent(source, _KEY_V, True)
    key_up = Quartz.CGEventCreateKeyboardEvent(source, _KEY_V, False)
    Quartz.CGEventSetFlags(key_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(key_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, key_down)
    Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, key_up)
