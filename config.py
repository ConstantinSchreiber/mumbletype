"""Mumbletype configuration persistence and cost tracking."""

import json
import os
import re
import threading
from datetime import datetime, timezone


class Config:
    """Central configuration backed by .env (API key) and config.json (everything else)."""

    MODELS = {
        "gpt-4o-mini-transcribe": {"rate_per_min": 0.003, "label": "GPT-4o Mini Transcribe"},
        "gpt-4o-transcribe": {"rate_per_min": 0.006, "label": "GPT-4o Transcribe"},
        "whisper-1": {"rate_per_min": 0.006, "label": "Whisper-1"},
    }

    _DIR = os.path.dirname(__file__)
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

    def get_audio_device(self) -> int | None:
        """Return the stored audio device index, or None for system default."""
        return self._data.get("audio_device")

    def set_audio_device(self, device: int | None):
        with self._lock:
            self._data["audio_device"] = device
            self._save()
        self._notify()

    # ── usage / cost tracking ────────────────────────────────────────────

    def record_usage(self, duration_seconds: float):
        model = self.get_model()
        rate = self.MODELS.get(model, {}).get("rate_per_min", 0.003)
        cost = (duration_seconds / 60.0) * rate
        with self._lock:
            usage = self._data.setdefault("usage", self._default_usage())
            usage["total_seconds"] += duration_seconds
            usage["total_cost_usd"] += cost
            usage["session_count"] += 1
            self._save()

    def get_usage(self) -> dict:
        return dict(self._data.get("usage", self._default_usage()))

    def reset_usage(self):
        with self._lock:
            self._data["usage"] = self._default_usage()
            self._save()
        self._notify()

    # ── change listeners ─────────────────────────────────────────────────

    def add_listener(self, callback):
        self._listeners.append(callback)

    def _notify(self):
        for cb in self._listeners:
            try:
                cb()
            except Exception:
                pass

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self):
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
        self._save()

    def _save(self):
        try:
            with open(self._CONFIG_PATH, "w") as f:
                json.dump(self._data, f, indent=2)
        except OSError:
            pass

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
            "last_reset": datetime.now(timezone.utc).isoformat(),
        }
