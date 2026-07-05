#!/usr/bin/env python3
"""Mumbletype – global voice-to-text input using OpenAI transcription models."""

import io
import logging
import logging.handlers
import os
import signal
import threading
import wave

import httpx
import numpy as np
from openai import OpenAI

LOG_PATH = os.path.expanduser("~/Library/Logs/Mumbletype.log")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=3)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    # httpx/openai request logs are noisy at INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


setup_logging()
log = logging.getLogger("mumbletype")

from clipboard import paste_text
from config import Config
from history import History
from mainthread import run_on_main
from recorder import CHANNELS, SAMPLE_RATE, Recorder

config = Config()
history = History()

# ── OpenAI client ────────────────────────────────────────────────────────
# Bounded timeout: the default (600s × retries) leaves the pill spinning for
# ~20 minutes on a network hang. keepalive_expiry=60 keeps the connection
# pre-warmed at recording start alive through a long dictation (httpx default
# expires it after 5 idle seconds).

_client: OpenAI | None = None
_client_api_key: str | None = None
_client_lock = threading.Lock()


def get_client() -> OpenAI:
    global _client, _client_api_key
    with _client_lock:
        api_key = config.get_api_key()
        if _client is None or api_key != _client_api_key:
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            timeout = httpx.Timeout(30.0, connect=5.0)
            _client = OpenAI(
                api_key=api_key,
                max_retries=1,
                timeout=timeout,
                http_client=httpx.Client(
                    timeout=timeout,
                    limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=60.0),
                ),
            )
            _client_api_key = api_key
        return _client


def _prewarm():
    """Warm DNS/TCP/TLS to the API while the user is speaking (~250ms saved
    on the transcription request after an idle period). Errors are irrelevant —
    even a failed call establishes the connection."""
    try:
        get_client().models.retrieve(config.get_model())
    except Exception:
        log.debug("prewarm request failed", exc_info=True)


# ── application ──────────────────────────────────────────────────────────


class App:
    """Wires hotkey → recorder → transcription → paste.

    UI session state (_ui_session) is owned by the main thread; recorder and
    transcription callbacks marshal themselves there. The session guard means
    a stale transcription finishing late can never hide or corrupt the pill
    of a newer recording.
    """

    def __init__(self, indicator):
        self.indicator = indicator
        self.status_bar = None  # attached in main() once the menu bar exists
        self.recorder = Recorder(
            config,
            on_started=self._recording_started,
            on_start_failed=self._recording_failed,
            on_stopped=self._recording_stopped,
            on_chunk=indicator.push_audio,
        )
        self._ui_session = 0

    # ── recorder callbacks (worker thread) ──────────────────────────────

    def _recording_started(self, sid):
        threading.Thread(target=_prewarm, name="prewarm", daemon=True).start()
        run_on_main(lambda: self._set_ui(sid, "recording"))

    def _recording_failed(self, sid):
        run_on_main(lambda: self._fail_ui(sid))

    def _recording_stopped(self, sid, frames):
        run_on_main(lambda: self._set_ui(sid, "transcribing"))
        threading.Thread(
            target=self._transcribe, args=(sid, frames),
            name=f"transcribe-{sid}", daemon=True,
        ).start()

    # ── UI state (main thread) ──────────────────────────────────────────

    def _set_ui(self, sid, state):
        self._ui_session = sid
        if state == "recording":
            self.indicator.show("recording")
        else:
            self.indicator.update(state)
        if self.status_bar:
            self.status_bar.update_status(state)

    def _fail_ui(self, sid):
        self._ui_session = sid
        self.indicator.flash_error()
        if self.status_bar:
            self.status_bar.update_status("idle")

    def _finish(self, sid, ok):
        if sid != self._ui_session:
            return  # a newer recording owns the pill
        if ok:
            self.indicator.hide()
        else:
            self.indicator.flash_error()
        if self.status_bar:
            self.status_bar.update_status("idle")

    # ── transcription (one thread per recording) ────────────────────────

    def _transcribe(self, sid, frames):
        ok = False
        try:
            ok = self._transcribe_and_paste(frames)
        except Exception:
            log.exception("transcription failed")
        run_on_main(lambda: self._finish(sid, ok))

    @staticmethod
    def _transcribe_and_paste(frames) -> bool:
        if not frames:
            log.warning("no audio captured")
            return False

        audio_data = np.concatenate(frames, axis=0)
        duration_seconds = len(audio_data) / SAMPLE_RATE

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # int16
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        buf.seek(0)
        buf.name = "recording.wav"

        result = get_client().audio.transcriptions.create(
            model=config.get_model(), file=buf
        )
        text = result.text.strip()
        if not text:
            log.warning("empty transcription")
            return False

        # Log to history before pasting: if the paste lands in the wrong
        # window (focus stolen mid-dictation), the text is still recoverable
        # from the menubar History submenu.
        history.add(text)
        paste_text(text)
        log.info("transcribed %.1fs: %s", duration_seconds, text)
        config.record_usage(duration_seconds)
        return True


def main():
    log.info("Mumbletype running · %s to record/stop · Ctrl+C to quit", config.get_hotkey()[2])
    log.info("model: %s", config.get_model())

    import AppKit
    from hotkey import HotkeyManager
    from indicator import Indicator
    from statusbar import StatusBarController

    ns_app = AppKit.NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    app = App(Indicator())

    # The hotkey callback fires on the main thread and only enqueues a toggle,
    # so it can neither block the event loop nor raise.
    hotkey_manager = HotkeyManager(config, on_fire=app.recorder.toggle)
    app.status_bar = StatusBarController(config, hotkey_manager, history)

    # Manually pump the event loop instead of app.run() so Python can handle
    # SIGINT (Ctrl+C). app.run() blocks in ObjC and never lets Python dispatch
    # signals. Events (incl. the Carbon hotkey) and timers fire while blocked
    # inside nextEventMatchingMask, so the timeout only bounds SIGINT latency.
    ns_app.finishLaunching()
    signal.signal(signal.SIGINT, lambda *_: ns_app.terminate_(None))

    from Foundation import NSDate, NSDefaultRunLoopMode

    while True:
        event = ns_app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            AppKit.NSEventMaskAny,
            NSDate.dateWithTimeIntervalSinceNow_(0.5),
            NSDefaultRunLoopMode,
            True,
        )
        if event is not None:
            ns_app.sendEvent_(event)


if __name__ == "__main__":
    main()
