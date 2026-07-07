# Mumbletype

macOS menubar app for global voice-to-text input powered by OpenAI's state-of-the-art transcription models. Press a hotkey anywhere, speak, and your words are typed at the cursor — accurately.

## Setup

Requires macOS and an [OpenAI API key](https://platform.openai.com/api-keys).

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Set your API key in Preferences after launching (or create
`~/Library/Application Support/Mumbletype/.env` containing `OPENAI_API_KEY=sk-...`).

## Usage

```bash
source venv/bin/activate
python mumbletype.py
```

- **⌃D** (configurable) — Hit once to record, hit again to transcribe and paste
- Click the menubar mic icon for model selection, history, usage stats, Start at Login, and preferences
- **History** submenu — every transcription is kept for 30 days; click an entry to copy it back to the clipboard (rescues text when a prompt or stray click steals focus and the paste goes astray)
- **Transcribe audio files** — drag audio files (e.g. iPhone Voice Memos) onto the menubar mic icon, or use *Transcribe Audio File…* from the menu. Multi-speaker recordings come back diarized as `Speaker A:` / `Speaker B:` turns. A small floating panel shows each file's progress and elapsed time so you can keep working; when a file finishes, its transcript is copied to the clipboard, kept in the **File Transcripts** submenu (click an entry to copy it out), and saved as `<name>.transcript.txt` next to the audio file. If your recordings are one language, pin it in Preferences → File Transcription Language — auto-detection can drift into translating long recordings
- Change the hotkey in Preferences → Record Hotkey → Change…

The hotkey is registered system-wide via Carbon `RegisterEventHotKey`: it is
consumed by Mumbletype and never reaches the focused app, and no Accessibility
or Input Monitoring permission is needed for listening. On first run macOS
will still prompt for **Microphone** access (recording) and **Accessibility**
(only for the synthetic ⌘V paste).

**Start at Login** (menubar toggle) installs a LaunchAgent that also restarts
the app automatically if it ever crashes. Quitting from the menu stays quit.

## Models

| Model | Cost | Notes |
|-------|------|-------|
| GPT-4o Mini Transcribe | $0.003/min | Default, fast and cheap |
| GPT-4o Transcribe | $0.006/min | Higher accuracy |
| Whisper-1 | $0.006/min | Original Whisper model |
| GPT-4o Transcribe Diarize + Whisper-1 | $0.012/min | Audio files only — speaker diarization + text |

Switch dictation models from the menubar dropdown or Preferences window.
Dropped audio files always use the diarization-capable model (override with
`"file_model"` in `config.json`).

## How it works

Mumbletype runs as a menubar-only app (no dock icon). The global hotkey
toggles recording with `sounddevice`; audio is sent in-memory to the OpenAI
transcription API and the result is pasted at the cursor via the clipboard +
a synthetic ⌘V. Your previous clipboard contents — including images and rich
text — are restored afterwards (unless you copied something in the meantime,
in which case your copy wins). A floating waveform pill appears bottom-center
of the screen your cursor is on during recording and transcription, and
flashes red if transcription fails.

Every transcription is also logged to a local history (before pasting, so
nothing is lost if the paste lands in the wrong window). The menubar History
submenu shows the recent entries; clicking one copies it to the clipboard.
Entries are pruned after 30 days.

Audio files (m4a, mp3, wav, flac, ogg, and anything else CoreAudio reads)
are transcribed in two concurrent passes: `whisper-1` produces the text
(the diarize model tends to *translate* non-English speech into English,
even with the language pinned; whisper stays in the source language) and
`gpt-4o-transcribe-diarize` produces the who-speaks-when timeline. The
timestamped whisper sentences are then attributed to speakers by overlap.
Recordings longer than the diarize model's ~23-minute per-request limit
are split into 10-minute chunks at quiet moments and transcribed in
parallel; short voice samples of each speaker from the first chunk are
passed along so "Speaker A" stays the same person across chunks.
Oddly-encoded files are re-encoded with the system's `afconvert` to fit
the API's 25 MB upload cap. File transcripts are never auto-pasted — they
land on the clipboard, in the File Transcripts menu, and in a
`.transcript.txt` beside the source file, with per-file progress in a
small floating panel. Dictation and file transcription costs are tracked
separately in the menubar.

Configuration lives in `~/Library/Application Support/Mumbletype/`; logs in
`~/Library/Logs/Mumbletype.log`.

## License

MIT
