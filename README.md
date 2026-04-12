# Mumbletype

macOS menubar app for global voice-to-text input powered by OpenAI's state-of-the-art transcription models. Press a hotkey anywhere, speak, and your words are typed at the cursor — accurately.

## Setup

Requires macOS and an [OpenAI API key](https://platform.openai.com/api-keys).

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz
```

Create a `.env` file with your API key (or set it later in Preferences):

```
OPENAI_API_KEY=sk-...
```

## Usage

```bash
source venv/bin/activate
python mumbletype.py
```

- **Ctrl+D** — Hit once to record, hit again to have it transcribed
- Click the menubar mic icon for model selection, usage stats, and preferences

On first run, macOS will prompt for **Accessibility** and **Microphone** permissions.

## Models

| Model | Cost | Notes |
|-------|------|-------|
| GPT-4o Mini Transcribe | $0.003/min | Default, fast and cheap |
| GPT-4o Transcribe | $0.006/min | Higher accuracy |
| Whisper-1 | $0.006/min | Original Whisper model |

Switch models from the menubar dropdown or Preferences window.

## How it works

Mumbletype runs as a menubar-only app (no dock icon). It listens for the global hotkey via `pynput`, records audio with `sounddevice`, sends it to the OpenAI transcription API, and pastes the result at your cursor position. A small floating indicator follows your mouse during recording and transcription.

## License

MIT
