"""Floating cursor-adjacent indicator window using AppKit."""

import threading

import AppKit
import Quartz
from Foundation import NSObject

# Strong references to pending trampolines so they aren't GC'd before execution
_pending = set()


class _Trampoline(NSObject):
    """Helper to dispatch a callable onto the main thread."""

    _blocks = {}

    def run_(self, _sender):
        block = self._blocks.pop(id(self), None)
        _pending.discard(self)
        if block:
            block()


def _on_main(block):
    t = _Trampoline.alloc().init()
    _Trampoline._blocks[id(t)] = block
    _pending.add(t)
    t.performSelectorOnMainThread_withObject_waitUntilDone_("run:", None, False)


def _cg_to_appkit(cg_x, cg_y):
    """Convert CG coordinates (top-left origin) to AppKit (bottom-left origin)."""
    primary_h = AppKit.NSScreen.screens()[0].frame().size.height
    return (cg_x, primary_h - cg_y)


class Indicator:
    """Tiny floating pill that follows the mouse cursor."""

    _COLORS = {
        "recording": (0.95, 0.22, 0.22, 0.92),
        "transcribing": (1.0, 0.60, 0.0, 0.92),
    }
    _LABELS = {
        "recording": "🎙",
        "transcribing": "⏳",
    }
    _SIZE = 32
    _OFFSET_X = 16
    _OFFSET_Y = -40  # above the cursor

    def __init__(self):
        self._window = None
        self._tap = None
        self._tap_source = None

    def show(self, state: str = "recording"):
        pos = self._get_mouse_pos()
        _on_main(lambda: self._show(state, pos))

    def update(self, state: str):
        _on_main(lambda: self._update(state))

    def hide(self):
        _on_main(self._hide)

    @staticmethod
    def _get_mouse_pos():
        cg_pos = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return _cg_to_appkit(cg_pos.x, cg_pos.y)

    # ── mouse tracking ──────────────────────────────────────────────────

    def _start_tracking(self):
        """Install a CG event tap to follow mouse moves."""
        if self._tap is not None:
            return

        def callback(_proxy, _type, event, _refcon):
            cg_pos = Quartz.CGEventGetLocation(event)
            appkit_pos = _cg_to_appkit(cg_pos.x, cg_pos.y)
            _on_main(lambda: self._move_to(appkit_pos))
            return event

        mask = (
            (1 << Quartz.kCGEventMouseMoved)
            | (1 << Quartz.kCGEventLeftMouseDragged)
            | (1 << Quartz.kCGEventRightMouseDragged)
        )
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            callback,
            None,
        )
        if self._tap is None:
            print("⚠  Could not create event tap for mouse tracking (need Accessibility permission)")
            return

        self._tap_source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetMain(),
            self._tap_source,
            Quartz.kCFRunLoopCommonModes,
        )

    def _stop_tracking(self):
        if self._tap_source is not None:
            Quartz.CFRunLoopRemoveSource(
                Quartz.CFRunLoopGetMain(),
                self._tap_source,
                Quartz.kCFRunLoopCommonModes,
            )
        if self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)
        self._tap = None
        self._tap_source = None

    def _move_to(self, pos):
        if self._window is None:
            return
        s = self._SIZE
        x = pos[0] + self._OFFSET_X
        y = pos[1] + self._OFFSET_Y
        self._window.setFrameOrigin_((x, y))

    # ── internals (run on main thread) ───────────────────────────────────

    def _make_window(self, state: str, pos: tuple[float, float]):
        label = self._LABELS[state]
        color = self._COLORS[state]
        s = self._SIZE

        x = pos[0] + self._OFFSET_X
        y = pos[1] + self._OFFSET_Y

        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((x, y), (s, s)),
            AppKit.NSWindowStyleMaskBorderless,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setLevel_(AppKit.NSFloatingWindowLevel + 2)
        window.setOpaque_(False)
        window.setBackgroundColor_(AppKit.NSColor.clearColor())
        window.setIgnoresMouseEvents_(True)
        window.setHasShadow_(True)

        # Rounded pill view
        view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (s, s)))
        view.setWantsLayer_(True)
        view.layer().setCornerRadius_(s / 2)
        ns_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color)
        view.layer().setBackgroundColor_(ns_color.CGColor())

        # Emoji label — centered
        tf = AppKit.NSTextField.labelWithString_(label)
        tf.setFont_(AppKit.NSFont.systemFontOfSize_(16))
        tf.setAlignment_(AppKit.NSTextAlignmentCenter)
        tf.setFrame_(((0, 0), (s, s)))
        intrinsic = tf.intrinsicContentSize()
        tf.setFrame_(((0, (s - intrinsic.height) / 2), (s, intrinsic.height)))
        view.addSubview_(tf)

        window.setContentView_(view)
        return window

    def _show(self, state: str, pos: tuple[float, float]):
        self._hide()
        self._window = self._make_window(state, pos)
        self._window.orderFrontRegardless()
        self._start_tracking()

    def _update(self, state: str):
        if self._window is None:
            pos = self._get_mouse_pos()
            self._show(state, pos)
            return
        color = self._COLORS[state]
        ns_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color)
        view = self._window.contentView()
        view.layer().setBackgroundColor_(ns_color.CGColor())
        for subview in view.subviews():
            if isinstance(subview, AppKit.NSTextField):
                subview.setStringValue_(self._LABELS[state])

    def _hide(self):
        self._stop_tracking()
        if self._window is not None:
            self._window.orderOut_(None)
            self._window = None
