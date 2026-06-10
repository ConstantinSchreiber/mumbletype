"""Worker-thread-owned audio recording state machine."""

import logging
import queue
import threading

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1


class _Capture:
    """Per-recording frame buffer; created on start, handed off by value on stop."""

    __slots__ = ("frames",)

    def __init__(self):
        self.frames: list[np.ndarray] = []


class Recorder:
    """Owns the sounddevice input stream on a dedicated worker thread.

    toggle() may be called from any thread; commands serialize through a queue
    so rapid hotkey presses cannot race. on_started/on_start_failed/on_stopped
    run on the worker thread (marshal UI work yourself); on_chunk runs on the
    audio thread.
    """

    def __init__(self, config, on_started, on_start_failed, on_stopped, on_chunk):
        self._config = config
        self._on_started = on_started
        self._on_start_failed = on_start_failed
        self._on_stopped = on_stopped
        self._on_chunk = on_chunk
        self._q = queue.SimpleQueue()
        self._stream = None
        self._stream_device_name = object()  # sentinel: never equals a config value
        self._capture = None  # written by worker thread, read by audio callback
        self._session = 0
        threading.Thread(target=self._run, name="recorder", daemon=True).start()

    def toggle(self):
        self._q.put("toggle")

    # ── worker thread ───────────────────────────────────────────────────

    def _run(self):
        recording = False
        while True:
            cmd = self._q.get()
            try:
                if cmd == "toggle":
                    if not recording:
                        recording = self._start()
                    else:
                        self._stop()
                        recording = False
            except Exception:
                log.exception("recorder command %r failed", cmd)
                recording = False

    def _start(self) -> bool:
        self._session += 1
        sid = self._session
        self._on_started(sid)  # pill up immediately; failure below undoes it
        try:
            self._ensure_stream()
            self._capture = _Capture()
            self._stream.start()
            log.info("recording started (session %d)", sid)
            return True
        except Exception:
            log.exception("failed to start recording")
            self._close_stream()  # drop the possibly-broken stream
            self._capture = None
            self._on_start_failed(sid)
            return False

    def _stop(self):
        sid = self._session
        try:
            # stop(), not abort(): waits for pending buffers so the last word
            # of speech is not clipped.
            self._stream.stop()
        except Exception:
            log.exception("stream.stop failed")
        capture, self._capture = self._capture, None
        frames = capture.frames if capture else []
        log.info("recording stopped (session %d, %d chunks)", sid, len(frames))
        self._on_stopped(sid, frames)

    def _resolve_device(self) -> tuple[int | None, str | None]:
        """Resolve the configured device name to a current PortAudio index."""
        want = self._config.get_audio_device_name()
        if want is None:
            return None, None
        try:
            for i, dev in enumerate(sd.query_devices()):
                if dev["name"] == want and dev["max_input_channels"] > 0:
                    return i, want
        except Exception:
            log.exception("audio device query failed")
        log.warning("audio device %r not found; using system default", want)
        return None, None

    def _ensure_stream(self):
        """(Re)create the stream iff the configured device changed.

        The name→index resolution happens at every start, so it survives
        PortAudio index shifts after unplug/replug — and because the device is
        only ever re-checked between recordings, a preferences change can never
        kill an active recording.
        """
        index, name = self._resolve_device()
        if self._stream is not None and name == self._stream_device_name:
            return
        self._close_stream()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=self._audio_cb,
            device=index,
        )
        self._stream_device_name = name

    def _close_stream(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            self._stream_device_name = object()

    # ── audio thread ────────────────────────────────────────────────────

    def _audio_cb(self, indata, frames, time_info, status):
        try:
            capture = self._capture  # atomic attribute read; None when idle
            if capture is not None:
                capture.frames.append(indata.copy())
                self._on_chunk(indata)
        except Exception:
            # An uncaught exception here would abort the stream silently.
            log.exception("audio callback failed")
