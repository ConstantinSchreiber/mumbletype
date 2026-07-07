# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Mumbletype

A macOS-only menubar app that provides global voice-to-text input using OpenAI transcription models. Press the record hotkey (default ⌃D, configurable) anywhere to record audio, press it again to transcribe via the OpenAI API and paste at the cursor position using clipboard + ⌘V. Audio files can also be dropped onto the menubar icon (or picked via the menu) for diarized multi-speaker transcription.

## Running

```bash
source venv/bin/activate   # ./venv is canonical; ./.venv is an empty stub
pip install -r requirements.txt
python mumbletype.py
```

Requires macOS Microphone access and Accessibility permission (only for posting the synthetic ⌘V — listening for the hotkey needs no permission). Logs go to `~/Library/Logs/Mumbletype.log` and stderr.

## Configuration

- Stored in `~/Library/Application Support/Mumbletype/`: `.env` (`OPENAI_API_KEY`), `config.json` (model, hotkey, audio device name, usage stats — dictation and file transcription tracked as separate buckets), and `history.json` (transcription history, 30-day retention)
- **Default model**: `gpt-4o-mini-transcribe` (also `gpt-4o-transcribe`, `whisper-1`)
- **File transcription model**: `gpt-4o-transcribe-diarize` (config key `file_model`, no UI) — kept out of `Config.MODELS` so the dictation model submenu doesn't offer it
- **File transcription language**: asked per batch in a picker dialog (config key `file_language` stores the last choice as the next default; "" = auto). Pinning matters: per-chunk auto-detection has translated German recordings into English mid-file
- Audio devices are persisted by **name** (PortAudio indices shift across replug/reboot)

## Architecture

All code lives in the project root (no packages/subfolders). There are no tests; verification is by running the app.

- **mumbletype.py** — Entry point and orchestrator. `App` owns UI session state (main thread) with a session guard so stale transcriptions can't disturb a newer recording's UI. File transcriptions live entirely in the jobs panel and never touch the pill, so dictation stays independent. OpenAI client is bounded (30s timeout, 1 retry, 60s keepalive) and the connection is pre-warmed when recording starts. Runs the AppKit event pump manually (0.5s tick, for SIGINT handling only — events/timers fire while blocked).
- **filetranscribe.py** — Audio-file transcription: every file is decoded to PCM (`afconvert`), split into 10-min chunks at quiet points (the diarize model rejects > 1400s and processes at ~real time), and each chunk gets two concurrent passes. `whisper-1` (verbose_json) supplies the TEXT — chosen after empirically eliminating every alternative on a real recording: `gpt-4o-transcribe` silently DROPS whole spans of multi-speaker audio, the diarize model translates foreign speech to English even with `language` pinned, and `gpt-audio` chat transcription 500s ("model produced invalid content") or truncates on real conversational audio. `gpt-4o-transcribe-diarize` (`diarized_json` + `chunking_strategy="auto"`) supplies the speaker timeline; whisper's timestamped segments are attributed to speakers by overlap (absorbs sub-second backchannel interjections). Labels renormalized A, B, C… (the API emits stray labels like `@`); single-speaker collapses to plain text. Chunk 1 runs first (its diarized segments yield 2-10s `known_speaker_references` data-URL clips), the rest in parallel (3 workers) — refs keep labels consistent across chunks; chunk 1's raw→canonical label map is computed once and reused at stitch time (re-mapping would split every speaker in two). Falls back to the plain dictation model if the API rejects the diarize model; 900s per-request timeout override. `App._transcribe_files` puts results in history (kind="file") + clipboard (plain copy, never auto-paste) + `<name>.transcript.txt` beside the source.
- **jobpanel.py** — `JobPanel`: minimal floating dark HUD (top-right, draggable, joins all Spaces) listing file-transcription jobs: live stage + elapsed per row, done → click to re-copy, failed → error tooltip. Closing hides it and clears finished rows; it re-shows when a job is added or finishes. Main-thread only — callers marshal via `run_on_main`.
- **hotkey.py** — `HotkeyManager`: global hotkey via Carbon `RegisterEventHotKey` (`quickmachotkey`). No event tap → macOS cannot silently disable it; the chord is consumed system-wide; callback fires on the main thread. Reconfigures live on config change; `pause()`/`resume()` used during shortcut capture. Also NSEvent→Carbon modifier mapping and display-string helpers.
- **recorder.py** — `Recorder`: dedicated worker thread owns the sounddevice stream; `toggle()` enqueues so rapid hotkey presses serialize. Per-recording capture buffer is handed off by value. Device name re-resolved to an index at every start (survives unplug; config changes never disturb an active recording).
- **clipboard.py** — `paste_text()`: snapshots ALL pasteboard types (fresh `NSPasteboardItem` copies — items belong to one pasteboard forever), posts ⌘V via CGEvent, restores after 1s only if `changeCount` is unchanged.
- **config.py** — `Config`: thread-safe singleton over `.env` + `config.json` (atomic writes). Change listeners may fire on ANY thread — marshal UI work via `mainthread.run_on_main`.
- **history.py** — `History`: thread-safe transcription log over `history.json` (atomic writes; same listener contract as Config). Entries carry an optional `kind` ("file" for audio-file transcripts, absent = dictation) and `label` (source filename). Every transcription is added *before* pasting so text survives a hijacked focus/paste. Pruned on load and add (30 days / 500 entries).
- **mainthread.py** — `run_on_main(block)`: trampoline to run a block on the AppKit main thread from any thread.
- **indicator.py** — Floating waveform pill (borderless NSWindow), bottom-center of the screen containing the cursor; recording/transcribing/error states with intro/outro animations.
- **statusbar.py** — `StatusBarController`: NSStatusItem menu built once; titles updated in place on the main thread. The History (dictations) and File Transcripts (audio files, titled by filename) submenus are the exception: repopulated on every open via `menuNeedsUpdate:` (clicking an entry copies it back to the clipboard — no paste, no restore; each submenu clears only its own kind). Start at Login toggle (see launchagent.py). `_DropTargetView` overlays the status-item button to accept audio-file drags (clicks are forwarded to the button so the menu still opens); "Transcribe Audio File…" opens an NSOpenPanel for the same flow.
- **launchagent.py** — LaunchAgent install/uninstall (`~/Library/LaunchAgents/com.mumbletype.plist`, `KeepAlive.SuccessfulExit=false` → crash restarts, clean quit stays quit). Never `bootout` the label from the launchd-managed instance itself.
- **preferences.py** — Programmatic AppKit preferences window (API key with async Test, model, audio device by name, hotkey capture via local NSEvent monitor — the global hotkey is paused during capture and always resumed).
- **setup.py** — py2app build script (untested deliverable).
- **debug_hotkey.py** — legacy pynput diagnostic (pynput is no longer a dependency).

### Key patterns

- All AppKit UI mutations must happen on the main thread — use `mainthread.run_on_main`.
- `Config` listeners (`config.add_listener`) may be invoked from any thread.
- Heavy work never runs on the main thread or in the hotkey callback: recording start/stop runs on the recorder worker; each transcription gets its own thread; completion marshals back to main where the session guard decides whether it still owns the UI.
- The app runs as `NSApplicationActivationPolicyAccessory` (no dock icon), switching to `Regular` only while the preferences window is open.
