"""Floating panel listing audio-file transcription jobs.

A minimal dark HUD (top-right of the main screen, draggable, follows Spaces)
so long transcriptions can run while the user works: one row per file with a
live stage + elapsed time; finished rows are clickable to re-copy the
transcript; failures show the error as a tooltip. Closing hides the panel
and clears finished rows — it reappears when a job is added or finishes.

All methods are main-thread only; callers marshal via mainthread.run_on_main.
"""

import logging
import os
import time

import AppKit
import objc
from Foundation import NSObject

from clipboard import copy_text

log = logging.getLogger(__name__)

_WIDTH = 340
_ROW_H = 30
_HEADER_H = 32
_PAD = 6
_MARGIN = 12
_MAX_FINISHED_KEPT = 10

_BG_COLOR = (0.12, 0.12, 0.14, 0.92)
_NAME_COLOR = AppKit.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92)
_TITLE_COLOR = AppKit.NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.55)
_RUNNING_COLOR = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.82, 0.55, 0.95)
_DONE_COLOR = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.90, 0.60, 0.95)
_FAILED_COLOR = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.40, 0.40, 0.95)


def _elapsed_str(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _label(text, size, color, align_right=False, bold=False):
    tf = AppKit.NSTextField.alloc().init()
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    font = (AppKit.NSFont.boldSystemFontOfSize_(size) if bold
            else AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                size, AppKit.NSFontWeightRegular))
    tf.setFont_(font)
    tf.setTextColor_(color)
    tf.setStringValue_(text)
    if align_right:
        tf.setAlignment_(AppKit.NSTextAlignmentRight)
    tf.cell().setLineBreakMode_(AppKit.NSLineBreakByTruncatingMiddle)
    return tf


class _PanelBackground(AppKit.NSView):
    def drawRect_(self, dirty):
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), 12.0, 12.0
        )
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*_BG_COLOR).setFill()
        path.fill()


class _RowView(AppKit.NSView):
    """One job row; a click re-copies a finished job's transcript."""

    def initWithFrame_(self, frame):
        self = objc.super(_RowView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._on_click = None
        return self

    def mouseDown_(self, event):
        if self._on_click is not None:
            self._on_click()
        else:
            # Let unfinished rows drag the window instead.
            objc.super(_RowView, self).mouseDown_(event)


class _PanelDelegate(NSObject):
    def initWithPanel_(self, panel):
        self = self.init()
        self._panel = panel
        return self

    def closePanel_(self, sender):
        self._panel._close()

    def tick_(self, timer):
        self._panel._tick()


class JobPanel:
    """Owns the job list and its window. Main thread only."""

    def __init__(self):
        self._jobs: list[dict] = []
        self._next_id = 1
        self._window = None
        self._content = None
        self._timer = None
        self._delegate = _PanelDelegate.alloc().initWithPanel_(self)
        self._status_fields: dict[int, AppKit.NSTextField] = {}

    # ── public API (main thread) ─────────────────────────────────────────

    def add(self, path: str) -> int:
        jid = self._next_id
        self._next_id += 1
        self._jobs.append({
            "id": jid,
            "name": os.path.basename(path),
            "state": "running",
            "msg": "queued…",
            "text": None,
            "error": None,
            "started": time.monotonic(),
        })
        self._prune()
        self._rebuild()
        self._show()
        return jid

    def progress(self, jid: int, msg: str):
        job = self._job(jid)
        if job is not None and job["state"] == "running":
            job["msg"] = msg
            self._update_status(job)

    def done(self, jid: int, text: str):
        job = self._job(jid)
        if job is None:
            return
        job.update(state="done", text=text, msg="✓ copied")
        self._rebuild()
        self._show()

    def fail(self, jid: int, error: str):
        job = self._job(jid)
        if job is None:
            return
        job.update(state="failed", error=error, msg="✗ failed")
        self._rebuild()
        self._show()

    # ── internals ────────────────────────────────────────────────────────

    def _job(self, jid):
        return next((j for j in self._jobs if j["id"] == jid), None)

    def _prune(self):
        finished = [j for j in self._jobs if j["state"] != "running"]
        for j in finished[: max(0, len(finished) - _MAX_FINISHED_KEPT)]:
            self._jobs.remove(j)

    def _close(self):
        if self._window is not None:
            self._window.orderOut_(None)
        self._jobs = [j for j in self._jobs if j["state"] == "running"]
        if self._jobs:
            self._rebuild()

    def _show(self):
        if self._window is None:
            self._make_window()
        if not self._window.isVisible():
            self._place_top_right()
        self._window.orderFrontRegardless()
        self._ensure_timer()

    def _status_text(self, job) -> str:
        if job["state"] == "running":
            return f"{job['msg']} {_elapsed_str(time.monotonic() - job['started'])}"
        return job["msg"]

    def _update_status(self, job):
        tf = self._status_fields.get(job["id"])
        if tf is not None:
            tf.setStringValue_(self._status_text(job))

    def _tick(self):
        running = [j for j in self._jobs if j["state"] == "running"]
        for job in running:
            self._update_status(job)
        if not running and self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def _ensure_timer(self):
        if self._timer is None and any(j["state"] == "running" for j in self._jobs):
            self._timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self._delegate, "tick:", None, True
            )

    # ── window / layout ──────────────────────────────────────────────────

    def _make_window(self):
        height = self._height()
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0, 0), (_WIDTH, height)),
            AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(AppKit.NSFloatingWindowLevel + 2)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        panel.setAppearance_(
            AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua)
        )
        self._content = _PanelBackground.alloc().initWithFrame_(((0, 0), (_WIDTH, height)))
        panel.setContentView_(self._content)
        self._window = panel
        self._rebuild()

    def _height(self) -> int:
        return _PAD + _HEADER_H + len(self._jobs) * _ROW_H + _PAD

    def _place_top_right(self):
        screen = AppKit.NSScreen.mainScreen() or AppKit.NSScreen.screens()[0]
        frame = screen.visibleFrame()
        x = frame.origin.x + frame.size.width - _WIDTH - _MARGIN
        y = frame.origin.y + frame.size.height - self._height() - _MARGIN
        self._window.setFrameOrigin_((x, y))

    def _rebuild(self):
        if self._window is None:
            return
        # Resize keeping the top edge anchored (the window may have been
        # dragged anywhere by the user).
        old = self._window.frame()
        top = old.origin.y + old.size.height
        height = self._height()
        self._window.setFrame_display_(
            ((old.origin.x, top - height), (_WIDTH, height)), True
        )

        for view in list(self._content.subviews()):
            view.removeFromSuperview()
        self._status_fields = {}

        # Header: title + close button
        title = _label("Transcriptions", 11, _TITLE_COLOR, bold=True)
        title.setFrame_(((14, height - _PAD - _HEADER_H + 8), (200, 16)))
        self._content.addSubview_(title)

        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "xmark", "Close"
        )
        close = AppKit.NSButton.buttonWithImage_target_action_(
            img, self._delegate, "closePanel:"
        )
        close.setBordered_(False)
        close.setContentTintColor_(_TITLE_COLOR)
        close.setFrame_(((_WIDTH - 30, height - _PAD - _HEADER_H + 6), (20, 20)))
        self._content.addSubview_(close)

        for i, job in enumerate(self._jobs):
            y = height - _PAD - _HEADER_H - (i + 1) * _ROW_H
            row = _RowView.alloc().initWithFrame_(((0, y), (_WIDTH, _ROW_H)))

            name = _label(job["name"], 12, _NAME_COLOR)
            name.setFrame_(((14, 7), (188, 16)))
            row.addSubview_(name)

            color = {"running": _RUNNING_COLOR, "done": _DONE_COLOR}.get(
                job["state"], _FAILED_COLOR
            )
            status = _label(self._status_text(job), 11, color, align_right=True)
            status.setFrame_(((204, 7), (_WIDTH - 204 - 14, 16)))
            row.addSubview_(status)
            self._status_fields[job["id"]] = status

            if job["state"] == "done":
                row._on_click = (lambda t=job["text"], j=job: self._recopy(j, t))
                row.setToolTip_("Click to copy the transcript again")
            elif job["state"] == "failed":
                row.setToolTip_(job["error"] or "transcription failed")

            self._content.addSubview_(row)
        self._content.setNeedsDisplay_(True)

    def _recopy(self, job, text):
        if text:
            copy_text(text)
            job["msg"] = "✓ copied"
            self._update_status(job)
