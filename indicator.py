"""Floating waveform indicator that follows the cursor during recording/transcription."""

import math
import threading

import AppKit
import objc
import Quartz
from Foundation import NSObject

import numpy as np

# ── helpers ─────────────────────────────────────────────────────────────────

_pending = set()  # prevent GC of trampolines


class _Trampoline(NSObject):
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
    Quartz.CFRunLoopWakeUp(Quartz.CFRunLoopGetMain())


# ── timer target ────────────────────────────────────────────────────────────


class _TimerTarget(NSObject):
    def initWithIndicator_(self, indicator):
        self = objc.super(_TimerTarget, self).init()
        self._indicator = indicator
        return self

    def tick_(self, timer):
        self._indicator._tick()


# ── waveform view ───────────────────────────────────────────────────────────


class WaveformView(AppKit.NSView):
    """Draws vertical rounded bars representing audio levels."""

    BAR_COUNT = 24
    BAR_WIDTH = 4.0
    BAR_GAP = 2.5
    BAR_RADIUS = 2.0
    MIN_BAR_H = 5.0
    PADDING_X = 7.0
    PADDING_Y = 7.0

    def initWithFrame_(self, frame):
        self = objc.super(WaveformView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._bar_heights = [0.0] * self.BAR_COUNT
        self._bar_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            1.0, 1.0, 1.0, 0.95
        )
        self._bg_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.12, 0.12, 0.14, 0.88
        )
        return self

    @objc.python_method
    def set_heights(self, heights):
        self._bar_heights = heights
        self.setNeedsDisplay_(True)

    @objc.python_method
    def set_bar_color(self, ns_color):
        self._bar_color = ns_color
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirty):
        bounds = self.bounds()
        w = bounds.size.width
        h = bounds.size.height
        usable_h = h - 2 * self.PADDING_Y

        # Dark pill background
        bg_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, h / 2.0, h / 2.0
        )
        self._bg_color.setFill()
        bg_path.fill()

        # Bars
        self._bar_color.setFill()
        for i, level in enumerate(self._bar_heights):
            bar_h = max(self.MIN_BAR_H, level * usable_h)
            x = self.PADDING_X + i * (self.BAR_WIDTH + self.BAR_GAP)
            y = (h - bar_h) / 2.0
            bar_rect = AppKit.NSMakeRect(x, y, self.BAR_WIDTH, bar_h)
            path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                bar_rect, self.BAR_RADIUS, self.BAR_RADIUS
            )
            path.fill()

    def isFlipped(self):
        return False


# ── indicator ───────────────────────────────────────────────────────────────

_WIDTH = 170
_HEIGHT = 42
_BOTTOM_MARGIN = 80

_BAR_COLORS = {
    "recording": AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        1.0, 1.0, 1.0, 0.95
    ),
    "transcribing": AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        1.0, 0.82, 0.55, 0.90
    ),
}


class Indicator:
    """Floating waveform pill that follows the mouse during recording."""

    BAR_COUNT = 24

    def __init__(self):
        self._window = None
        self._waveform_view = None
        self._timer = None
        self._timer_target = _TimerTarget.alloc().initWithIndicator_(self)
        self._state = None
        self._visible = False
        self._levels_lock = threading.Lock()
        self._levels = [0.0] * self.BAR_COUNT
        self._smooth = [0.0] * self.BAR_COUNT  # display-smoothed heights
        self._anim_phase = 0.0
        self._peak = 0.0  # adaptive gain: tracked peak RMS
        self._PEAK_DECAY = 0.95  # how fast the peak envelope decays per chunk
        self._PEAK_FLOOR = 0.01  # minimum peak to avoid amplifying silence/noise
        self._ATTACK = 0.45  # how fast bars rise (per tick)
        self._RELEASE = 0.12  # how fast bars fall (per tick)
        self._intro_t = 1.0  # intro animation progress (0→1), starts done
        self._intro_target = (0, 0)
        self._outro_t = 1.0  # outro animation progress (0→1), starts done
        self._outro_origin = (0, 0)

    # ── public API ──────────────────────────────────────────────────────

    def show(self, state="recording"):
        _on_main(lambda: self._show(state))

    def update(self, state):
        _on_main(lambda: self._update(state))

    def hide(self):
        _on_main(self._hide)

    def push_audio(self, chunk: np.ndarray):
        """Called from audio thread. chunk is int16 mono ndarray."""
        rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2)) / 32768.0
        # Adaptive gain: track a decaying peak envelope, scale relative to it
        self._peak = max(rms, self._peak * self._PEAK_DECAY)
        effective_peak = max(self._peak, self._PEAK_FLOOR)
        level = min(1.0, (rms / effective_peak) * 0.8)
        with self._levels_lock:
            self._levels.append(level)
            self._levels.pop(0)

    # ── animation ───────────────────────────────────────────────────────

    def _start_animation(self):
        if self._timer is not None:
            return
        self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self._timer_target, "tick:", None, True
        )

    def _stop_animation(self):
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _tick(self):
        if self._waveform_view is None:
            return
        # Drive outro animation (fade + slide down over ~150ms)
        if self._outro_t < 1.0:
            self._outro_t = min(1.0, self._outro_t + 0.05 / 0.15)
            # Ease-in: t' = t^2
            t = self._outro_t ** 2
            self._window.setAlphaValue_(1.0 - t)
            ox, oy = self._outro_origin
            self._window.setFrameOrigin_((ox, oy - 12 * t))
            if self._outro_t >= 1.0:
                self._window.orderOut_(None)
                self._window.setAlphaValue_(1.0)
                self._stop_animation()
            return
        # Drive intro animation (fade + slide up over ~150ms)
        if self._intro_t < 1.0:
            self._intro_t = min(1.0, self._intro_t + 0.05 / 0.15)  # 0.05s tick / 0.15s duration
            # Ease-out: t' = 1 - (1-t)^2
            t = 1.0 - (1.0 - self._intro_t) ** 2
            self._window.setAlphaValue_(t)
            tx, ty = self._intro_target
            self._window.setFrameOrigin_((tx, ty - 12 * (1.0 - t)))
        if self._state == "recording":
            with self._levels_lock:
                targets = list(self._levels)
            for i in range(self.BAR_COUNT):
                if targets[i] > self._smooth[i]:
                    self._smooth[i] += (targets[i] - self._smooth[i]) * self._ATTACK
                else:
                    self._smooth[i] += (targets[i] - self._smooth[i]) * self._RELEASE
            self._waveform_view.set_heights(list(self._smooth))
        elif self._state == "transcribing":
            self._anim_phase += 0.15
            heights = []
            for i in range(self.BAR_COUNT):
                v = 0.3 + 0.25 * math.sin(self._anim_phase + i * 0.35)
                v += 0.15 * math.sin(self._anim_phase * 0.7 + i * 0.55)
                heights.append(max(0.0, min(1.0, v)))
            self._waveform_view.set_heights(heights)

    # ── window management ───────────────────────────────────────────────

    @staticmethod
    def _bottom_center_pos():
        """Compute bottom-center position on the screen containing the cursor."""
        screen = AppKit.NSScreen.mainScreen()
        frame = screen.visibleFrame()
        x = frame.origin.x + (frame.size.width - _WIDTH) / 2.0
        y = frame.origin.y + _BOTTOM_MARGIN
        return (x, y)

    def _show(self, state):
        self._state = state
        with self._levels_lock:
            self._levels = [0.0] * self.BAR_COUNT
        self._smooth = [0.0] * self.BAR_COUNT
        self._anim_phase = 0.0
        self._peak = 0.0
        pos = self._bottom_center_pos()
        if self._window is None:
            self._make_window(pos)
        else:
            self._window.setFrameOrigin_(pos)
        color = _BAR_COLORS.get(state, _BAR_COLORS["recording"])
        self._waveform_view.set_bar_color(color)
        self._waveform_view.set_heights([0.0] * self.BAR_COUNT)

        # Animate in: fade + slide up over ~150ms (driven by the waveform timer)
        self._window.setAlphaValue_(0.0)
        self._window.setFrameOrigin_((pos[0], pos[1] - 12))
        self._intro_target = pos
        self._intro_t = 0.0
        self._window.orderFrontRegardless()

        self._visible = True
        self._start_animation()

    def _update(self, state):
        self._state = state
        with self._levels_lock:
            self._levels = [0.0] * self.BAR_COUNT
        self._anim_phase = 0.0
        if self._visible and self._waveform_view is not None:
            color = _BAR_COLORS.get(state, _BAR_COLORS["recording"])
            self._waveform_view.set_bar_color(color)
        else:
            self._show(state)

    def _hide(self):
        self._visible = False
        if self._window is not None:
            origin = self._window.frame().origin
            self._outro_origin = (origin.x, origin.y)
            self._outro_t = 0.0
            # Keep animation running to drive the outro
            self._start_animation()
        else:
            self._stop_animation()

    def _make_window(self, pos):
        frame = (pos, (_WIDTH, _HEIGHT))

        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            AppKit.NSWindowStyleMaskBorderless,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        window.setLevel_(AppKit.NSFloatingWindowLevel + 2)
        window.setOpaque_(False)
        window.setBackgroundColor_(AppKit.NSColor.clearColor())
        window.setIgnoresMouseEvents_(True)
        window.setHasShadow_(True)

        waveform = WaveformView.alloc().initWithFrame_(((0, 0), (_WIDTH, _HEIGHT)))
        color = _BAR_COLORS.get(self._state, _BAR_COLORS["recording"])
        waveform.set_bar_color(color)

        window.setContentView_(waveform)
        self._window = window
        self._waveform_view = waveform
