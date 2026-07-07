"""Persistent transcription history with retention-based pruning."""

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

RETENTION_DAYS = 30
MAX_ENTRIES = 500


class History:
    """Thread-safe transcription log backed by history.json (atomic writes).

    Entries are stored oldest-first on disk as {"ts": <ISO-8601 UTC>, "text": str}.
    Pruning (age + count) runs on load and on every add. Like Config, change
    listeners may fire on ANY thread — marshal UI work via mainthread.run_on_main.
    """

    _DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Mumbletype")
    _PATH = os.path.join(_DIR, "history.json")

    def __init__(self):
        self._lock = threading.Lock()
        self._listeners: list = []
        self._entries: list[dict] = []
        self._load()

    # ── public API ───────────────────────────────────────────────────────

    def add(self, text: str, kind: str = "dictation", label: str | None = None):
        """kind separates dictations from file transcripts (entries without a
        kind predate the split and count as dictations); label is a display
        name, e.g. the source filename."""
        entry: dict = {"ts": datetime.now(timezone.utc).isoformat(), "text": text}
        if kind != "dictation":
            entry["kind"] = kind
        if label:
            entry["label"] = label
        with self._lock:
            self._entries.append(entry)
            self._prune()
            self._save()
        self._notify()

    def entries(self, kind: str | None = None) -> list[dict]:
        """Return entries newest-first, optionally filtered by kind."""
        with self._lock:
            entries = list(reversed(self._entries))
        if kind is None:
            return entries
        return [e for e in entries if e.get("kind", "dictation") == kind]

    def clear(self, kind: str | None = None):
        """Drop all entries, or only those of the given kind."""
        with self._lock:
            if kind is None:
                self._entries = []
            else:
                self._entries = [
                    e for e in self._entries if e.get("kind", "dictation") != kind
                ]
            self._save()
        self._notify()

    def add_listener(self, callback):
        self._listeners.append(callback)

    def _notify(self):
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("history listener failed")

    # ── persistence ──────────────────────────────────────────────────────

    def _load(self):
        os.makedirs(self._DIR, exist_ok=True)
        if os.path.exists(self._PATH):
            try:
                with open(self._PATH, "r") as f:
                    data = json.load(f)
                self._entries = [
                    e for e in data
                    if isinstance(e, dict) and isinstance(e.get("text"), str)
                ]
            except (json.JSONDecodeError, OSError):
                log.exception("failed to load history; starting empty")
                self._entries = []
        before = len(self._entries)
        self._prune()
        if len(self._entries) != before:
            self._save()

    def _prune(self):
        """Drop entries past retention or over the count cap. Caller holds the
        lock (or is __init__). Timestamps share one format, so string compare
        is chronological."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
        self._entries = [e for e in self._entries if e.get("ts", "") >= cutoff]
        self._entries = self._entries[-MAX_ENTRIES:]

    def _save(self):
        try:
            tmp = self._PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._entries, f, indent=2)
            os.replace(tmp, self._PATH)
        except OSError:
            log.exception("failed to save history")
