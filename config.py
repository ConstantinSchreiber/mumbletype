"""Mumbletype configuration persistence and cost tracking."""

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Default record hotkey: Ctrl+D (Carbon virtual key / modifier mask).
DEFAULT_HOTKEY_VIRTUAL_KEY = 0x02  # kVK_ANSI_D
DEFAULT_HOTKEY_MODIFIER_MASK = 0x1000  # controlKey
DEFAULT_HOTKEY_DISPLAY = "⌃D"


class Config:
    """Central configuration backed by .env (API key) and config.json (everything else)."""

    MODELS = {
        "gpt-4o-mini-transcribe": {"rate_per_min": 0.003, "label": "GPT-4o Mini Transcribe"},
        "gpt-4o-transcribe": {"rate_per_min": 0.006, "label": "GPT-4o Transcribe"},
        "whisper-1": {"rate_per_min": 0.006, "label": "Whisper-1"},
    }

    # Used for dropped/picked audio files (not live dictation): supports
    # speaker diarization. Not in MODELS — it would be waste on 5s dictations.
    # Rate covers both passes: diarize ($0.006/min, speaker timeline) +
    # whisper-1 ($0.006/min, text — every OpenAI alternative fails on real
    # conversations: 4o-transcribe drops spans, diarize translates,
    # gpt-audio 500s/truncates).
    FILE_MODELS = {
        "gpt-4o-transcribe-diarize": {"rate_per_min": 0.012, "label": "GPT-4o Transcribe Diarize"},
    }

    # Language pin for file transcription ("" = auto-detect), asked per batch
    # in a picker dialog; this stores the last choice as the next default.
    # Pinning matters: auto-detection can flip mid-file — German recordings
    # have come back partially translated into English.
    FILE_LANGUAGES = {
        "": "Auto-detect",
        "de": "Deutsch",
        "en": "English",
        "fr": "Français",
        "es": "Español",
        "it": "Italiano",
    }

    _DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Mumbletype")
    _CONFIG_PATH = os.path.join(_DIR, "config.json")
    _ENV_PATH = os.path.join(_DIR, ".env")

    def __init__(self):
        self._lock = threading.Lock()
        self._listeners: list = []
        self._data: dict = {}
        self._load()

    # ── public API ───────────────────────────────────────────────────────

    def get_api_key(self) -> str:
        return os.environ.get("OPENAI_API_KEY", "")

    def set_api_key(self, key: str):
        """Persist API key to .env and update the process environment."""
        os.environ["OPENAI_API_KEY"] = key
        self._write_env_key(key)
        self._notify()

    def get_model(self) -> str:
        return self._data.get("model", "gpt-4o-mini-transcribe")

    def set_model(self, model: str):
        with self._lock:
            self._data["model"] = model
            self._save()
        self._notify()

    def get_file_model(self) -> str:
        """Model for transcribing dropped audio files (diarization-capable)."""
        return self._data.get("file_model", "gpt-4o-transcribe-diarize")

    def get_file_language(self) -> str:
        """ISO-639-1 language pin for file transcription; "" = auto-detect."""
        return self._data.get("file_language", "")

    def set_file_language(self, language: str):
        with self._lock:
            self._data["file_language"] = language
            self._save()
        self._notify()

    def get_audio_device_name(self) -> str | None:
        """Return the stored audio input device name, or None for system default."""
        return self._data.get("audio_device_name")

    def set_audio_device_name(self, name: str | None):
        with self._lock:
            self._data["audio_device_name"] = name
            self._save()
        self._notify()

    def get_hotkey(self) -> tuple[int, int, str]:
        """Return (virtual_key, carbon_modifier_mask, display_string)."""
        hk = self._data.get("hotkey") or {}
        return (
            hk.get("virtualKey", DEFAULT_HOTKEY_VIRTUAL_KEY),
            hk.get("modifierMask", DEFAULT_HOTKEY_MODIFIER_MASK),
            hk.get("display", DEFAULT_HOTKEY_DISPLAY),
        )

    def set_hotkey(self, virtual_key: int, modifier_mask: int, display: str):
        with self._lock:
            self._data["hotkey"] = {
                "virtualKey": virtual_key,
                "modifierMask": modifier_mask,
                "display": display,
            }
            self._save()
        self._notify()

    # ── usage / cost tracking ────────────────────────────────────────────

    def record_usage(self, duration_seconds: float):
        """Record dictation usage (the hotkey flow)."""
        model = self.get_model()
        rate = self.MODELS.get(model, {}).get("rate_per_min", 0.003)
        cost = (duration_seconds / 60.0) * rate
        with self._lock:
            usage = self._data.setdefault("usage", self._default_usage())
            usage["total_seconds"] += duration_seconds
            usage["total_cost_usd"] += cost
            usage["session_count"] += 1
            self._save()
        self._notify()  # keep the menubar usage line fresh

    def record_file_usage(self, duration_seconds: float):
        """Record audio-file transcription usage — its own bucket so the
        menu can show dictation and file costs separately."""
        model = self.get_file_model()
        rate = self.FILE_MODELS.get(model, {}).get("rate_per_min", 0.006)
        cost = (duration_seconds / 60.0) * rate
        with self._lock:
            usage = self._data.setdefault("usage", self._default_usage())
            usage["file_seconds"] = usage.get("file_seconds", 0.0) + duration_seconds
            usage["file_cost_usd"] = usage.get("file_cost_usd", 0.0) + cost
            usage["file_count"] = usage.get("file_count", 0) + 1
            self._save()
        self._notify()

    def get_usage(self) -> dict:
        return dict(self._data.get("usage", self._default_usage()))

    def reset_usage(self):
        with self._lock:
            self._data["usage"] = self._default_usage()
            self._save()
        self._notify()

    # ── change listeners ─────────────────────────────────────────────────

    def add_listener(self, callback):
        """Register a change callback. Callbacks may be invoked from ANY thread —
        marshal UI work to the main thread yourself (see mainthread.run_on_main)."""
        self._listeners.append(callback)

    def _notify(self):
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("config listener failed")

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self):
        os.makedirs(self._DIR, exist_ok=True)

        # Migrate old config files from project directory if they exist
        old_dir = os.path.dirname(os.path.abspath(__file__))
        for fname in ("config.json", ".env"):
            old_path = os.path.join(old_dir, fname)
            new_path = os.path.join(self._DIR, fname)
            if os.path.exists(old_path) and not os.path.exists(new_path):
                import shutil
                shutil.copy2(old_path, new_path)

        # Load .env for API key
        from dotenv import load_dotenv
        load_dotenv(self._ENV_PATH)

        # Load config.json
        if os.path.exists(self._CONFIG_PATH):
            try:
                with open(self._CONFIG_PATH, "r") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}

        # Ensure defaults
        self._data.setdefault("model", os.environ.get("TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe"))
        self._data.setdefault("usage", self._default_usage())
        self._migrate_audio_device()
        self._save()

    def _migrate_audio_device(self):
        """Convert legacy PortAudio device index to a device name (indices shift
        when devices are plugged/unplugged; names are stable)."""
        if "audio_device" not in self._data:
            return
        index = self._data.pop("audio_device")
        if isinstance(index, int) and "audio_device_name" not in self._data:
            try:
                import sounddevice as sd
                self._data["audio_device_name"] = sd.query_devices(index)["name"]
            except Exception:
                log.warning("could not resolve legacy audio device index %r; using default", index)

    def _save(self):
        try:
            tmp = self._CONFIG_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._CONFIG_PATH)
        except OSError:
            log.exception("failed to save config")

    def _write_env_key(self, key: str):
        """Update or create the OPENAI_API_KEY line in .env."""
        lines = []
        found = False
        if os.path.exists(self._ENV_PATH):
            with open(self._ENV_PATH, "r") as f:
                lines = f.readlines()
            for i, line in enumerate(lines):
                if re.match(r"^\s*OPENAI_API_KEY\s*=", line):
                    lines[i] = f"OPENAI_API_KEY={key}\n"
                    found = True
                    break
        if not found:
            lines.append(f"OPENAI_API_KEY={key}\n")
        with open(self._ENV_PATH, "w") as f:
            f.writelines(lines)

    @staticmethod
    def _default_usage() -> dict:
        return {
            "total_seconds": 0.0,
            "total_cost_usd": 0.0,
            "session_count": 0,
            "file_seconds": 0.0,
            "file_cost_usd": 0.0,
            "file_count": 0,
            "last_reset": datetime.now(timezone.utc).isoformat(),
        }
