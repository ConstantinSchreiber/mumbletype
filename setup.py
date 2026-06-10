"""py2app build script for Mumbletype."""

from setuptools import setup

APP = ["mumbletype.py"]
DATA_FILES = []

OPTIONS = {
    "argv_emulation": False,
    "packages": [
        "pynput",
        "sounddevice",
        "numpy",
        "openai",
        "dotenv",
        "_sounddevice_data",
    ],
    "includes": [
        "AppKit",
        "Quartz",
        "Foundation",
        "objc",
    ],
    "plist": {
        "CFBundleName": "Mumbletype",
        "CFBundleDisplayName": "Mumbletype",
        "CFBundleIdentifier": "com.mumbletype.app",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # No dock icon (menubar app)
        "NSMicrophoneUsageDescription": "Mumbletype needs microphone access to record audio for transcription.",
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
