# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Mumbletype

A macOS-only menubar app that provides global voice-to-text input using OpenAI transcription models. Press Ctrl+D anywhere to record audio, which is transcribed via the OpenAI API and typed at the cursor position using clipboard + Cmd-V.

## Running

```bash
source venv/bin/activate
pip install -r requirements.txt   # pynput, sounddevice, numpy, openai, python-dotenv
python mumbletype.py
```

Requires macOS Accessibility permission (for keyboard listener and event tap) and microphone access. Uses pyobjc (AppKit, Quartz, Foundation) which must be installed in the venv but is not listed in requirements.txt.

## Configuration

- **API key**: stored in `.env` as `OPENAI_API_KEY`
- **Model and settings**: stored in `config.json` (model, audio_device, usage stats)
- **Default model**: `gpt-4o-mini-transcribe` (also supports `gpt-4o-transcribe` and `whisper-1`)

## Architecture

All code lives in the project root (no packages/subfolders). There are no tests.

- **mumbletype.py** — Entry point. Runs the AppKit event loop on main thread, keyboard listener (pynput) on background thread. Handles recording via `sounddevice`, transcription via OpenAI API, and text insertion via `pbcopy`/`osascript` Cmd-V.
- **config.py** — `Config` class: thread-safe singleton managing `.env` (API key) and `config.json` (model, audio device, usage/cost tracking). Supports change listeners that other components subscribe to.
- **indicator.py** — `Indicator` class: floating AppKit pill window that follows the mouse cursor during recording/transcription. Uses CG event tap for mouse tracking, dispatches UI work to main thread via `_Trampoline` NSObject helper.
- **statusbar.py** — `StatusBarController`: macOS menu bar icon with dropdown for model selection, usage stats, and preferences access. Delegates actions via `_MenuDelegate` NSObject.
- **preferences.py** — `PreferencesWindowController`: native AppKit preferences window for API key, model, audio device selection. All UI built programmatically (no nibs/storyboards).

### Key patterns

- All AppKit UI mutations must happen on the main thread — `indicator.py` uses `_on_main()` trampoline pattern for this.
- `Config` uses a listener/observer pattern — call `config.add_listener(callback)` to get notified on config changes.
- The app runs as `NSApplicationActivationPolicyAccessory` (no dock icon), switching to `Regular` only when preferences window opens.
