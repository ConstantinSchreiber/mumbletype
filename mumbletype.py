#!/usr/bin/env python3
"""Mumbletype – global voice-to-text input using OpenAI transcription models."""

import io
import os
import signal
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
    indicator.push_audio(indata)


def _ensure_stream():
    """Create or reuse the audio input stream (avoids repeated device opens)."""
    global stream
    if stream is not None:
        return
    device = config.get_audio_device()
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        callback=audio_callback,
        device=device,
    )


def _invalidate_stream():
    """Called when audio device config changes — forces stream re-creation."""
    global stream
    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        stream = None


config.add_listener(_invalidate_stream)


def start_recording():
    global recording, audio_frames
    audio_frames = []
    recording = True
    indicator.show("recording")
    if status_bar:
        status_bar.update_status("recording")
    _ensure_stream()
    stream.start()
    print("⏺  Recording…")


def stop_recording():
    global recording
    if stream is not None:
        stream.stop()
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

    type_text(text)
    indicator.hide()
    if status_bar:
        status_bar.update_status("idle")
    print(f"✓  {text}")
    config.record_usage(duration_seconds)


def type_text(text: str):
    """Type text at the current cursor position via the clipboard + Cmd-V."""
    import AppKit
    import Quartz

    pb = AppKit.NSPasteboard.generalPasteboard()

    # Save current clipboard
    old_clip = pb.stringForType_(AppKit.NSPasteboardTypeString)

    # Set clipboard to transcribed text
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)

    # Simulate Cmd-V via CGEvent (much faster than osascript subprocess)
    source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    cmd_down = Quartz.CGEventCreateKeyboardEvent(source, 0x09, True)   # 0x09 = 'v'
    cmd_up = Quartz.CGEventCreateKeyboardEvent(source, 0x09, False)
    Quartz.CGEventSetFlags(cmd_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(cmd_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, cmd_down)
    Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, cmd_up)

    # Restore old clipboard after a short delay
    if old_clip is not None:
        def restore():
            import time
            time.sleep(0.5)
            pb.clearContents()
            pb.setString_forType_(old_clip, AppKit.NSPasteboardTypeString)
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

    print("Mumbletype running  ·  Ctrl+D to record/stop  ·  Ctrl+C to quit")
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
            NSDate.dateWithTimeIntervalSinceNow_(0.05),
            NSDefaultRunLoopMode,
            True,
        )
        if event is not None:
            app.sendEvent_(event)


if __name__ == "__main__":
    main()
