"""Diagnostic: prints every key pynput sees. Run from the same venv/terminal as mumbletype.

  source venv/bin/activate
  python debug_hotkey.py

Press a few keys (including Ctrl+D), then Ctrl+C to quit.
If you see NOTHING for any key: it's a macOS permission / binary issue.
If you see key events but Ctrl+D looks weird: it's a pynput/hotkey issue.
"""
import sys
import threading
import time
from pynput import keyboard

print(f"python: {sys.executable}")
print("Listening for keys. Press some keys, then Ctrl+C.\n")

seen_any = threading.Event()

def on_press(key):
    seen_any.set()
    print(f"PRESS   {key!r}")

def on_release(key):
    print(f"RELEASE {key!r}")

listener = keyboard.Listener(on_press=on_press, on_release=on_release)
listener.start()
print(f"listener.running = {listener.running}")

try:
    # Warn after 5s if no events ever arrived.
    time.sleep(5)
    if not seen_any.is_set():
        print("\n!! 5s elapsed and pynput received ZERO key events.")
        print("!! This is the macOS-permission silent-fail signature.")
        print("!! Check: System Settings -> Privacy & Security -> Input Monitoring")
        print("!! (separate from Accessibility) for the app launching this Python.\n")
    listener.join()
except KeyboardInterrupt:
    pass
