#!/usr/bin/env python3
"""WhisprFlow – global voice-to-text input using OpenAI transcription models."""

import io
import os
import signal
import subprocess
import sys
import threading
import wave

import numpy as np
import sounddevice as sd
from openai import OpenAI
from pynput import keyboard

sys.path.insert(0, os.path.dirname(__file__))
from config import Config
from indicator import Indicator

config = Config()

_client: OpenAI | None = None

def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.get_api_key())
    return _client

def refresh_client():
    global _client
    _client = None

config.add_listener(refresh_client)

SAMPLE_RATE = 16000
CHANNELS = 1
HOTKEY = {keyboard.Key.ctrl_l, keyboard.KeyCode.from_char("d")}

# ── state ────────────────────────────────────────────────────────────────
recording = False
audio_frames: list[np.ndarray] = []
stream: sd.InputStream | None = None
current_keys: set = set()
lock = threading.Lock()
indicator = Indicator()
status_bar = None  # set in main()


def audio_callback(indata, frames, time_info, status):
    audio_frames.append(indata.copy())


def start_recording():
    global recording, stream, audio_frames
    audio_frames = []
    device = config.get_audio_device()
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        callback=audio_callback,
        device=device,
    )
    stream.start()
    recording = True
    indicator.show("recording")
    if status_bar:
        status_bar.update_status("recording")
    print("⏺  Recording…")


def stop_recording():
    global recording, stream
    if stream is not None:
        stream.stop()
        stream.close()
        stream = None
    recording = False
    indicator.update("transcribing")
    if status_bar:
        status_bar.update_status("transcribing")
    print("⏹  Stopped. Transcribing…")
    threading.Thread(target=transcribe_and_type, daemon=True).start()


def transcribe_and_type():
    if not audio_frames:
        print("⚠  No audio captured.")
        indicator.hide()
        if status_bar:
            status_bar.update_status("idle")
        return

    audio_data = np.concatenate(audio_frames, axis=0)
    duration_seconds = len(audio_data) / SAMPLE_RATE

    # Write to WAV in memory
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # int16
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_data.tobytes())
    buf.seek(0)
    buf.name = "recording.wav"

    try:
        result = get_client().audio.transcriptions.create(
            model=config.get_model(), file=buf
        )
        text = result.text.strip()
    except Exception as e:
        print(f"✗  Transcription error: {e}")
        indicator.hide()
        if status_bar:
            status_bar.update_status("idle")
        return

    if not text:
        print("⚠  Empty transcription.")
        indicator.hide()
        if status_bar:
            status_bar.update_status("idle")
        return

    config.record_usage(duration_seconds)
    print(f"✓  {text}")
    indicator.hide()
    if status_bar:
        status_bar.update_status("idle")
    type_text(text)


def type_text(text: str):
    """Type text at the current cursor position via the clipboard + Cmd-V."""
    # Save current clipboard
    try:
        old_clip = subprocess.run(
            ["pbpaste"], capture_output=True, text=True
        ).stdout
    except Exception:
        old_clip = None

    # Set clipboard to transcribed text
    subprocess.run(["pbcopy"], input=text, text=True)

    # Simulate Cmd-V
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to keystroke "v" using command down',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Restore old clipboard after a short delay
    if old_clip is not None:
        def restore():
            import time
            time.sleep(0.5)
            subprocess.run(["pbcopy"], input=old_clip, text=True)
        threading.Thread(target=restore, daemon=True).start()


def on_press(key):
    current_keys.add(key)
    if HOTKEY.issubset(current_keys):
        with lock:
            if not recording:
                start_recording()
            else:
                stop_recording()
        current_keys.clear()


def on_release(key):
    current_keys.discard(key)


def main():
    global status_bar

    print("WhisprFlow running  ·  Ctrl+D to record/stop  ·  Ctrl+C to quit")
    print(f"Model: {config.get_model()}")

    # Start keyboard listener on a background thread
    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.start()

    # Run the AppKit run loop on the main thread (required for window rendering)
    import AppKit
    from statusbar import StatusBarController

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    status_bar = StatusBarController(config)

    # Manually pump the event loop instead of app.run() so Python can handle
    # SIGINT (Ctrl+C). app.run() blocks in ObjC and never lets Python dispatch signals.
    app.finishLaunching()
    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))

    from Foundation import NSDate, NSDefaultRunLoopMode

    while True:
        event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            AppKit.NSEventMaskAny,
            NSDate.dateWithTimeIntervalSinceNow_(0.5),
            NSDefaultRunLoopMode,
            True,
        )
        if event is not None:
            app.sendEvent_(event)


if __name__ == "__main__":
    main()
