"""Audio-file transcription with speaker diarization.

Files dropped on the menubar icon (or picked via the menu) are sent to
OpenAI's diarization-capable transcription model. Multi-speaker audio is
rendered as "Speaker A: …" turns; single-speaker audio as plain text.

The diarize model rejects audio over 1400s, so long files are split
client-side into ~20-minute chunks at quiet points. Speaker labels stay
consistent across chunks by passing short reference clips of each speaker
(extracted from earlier chunks) via known_speaker_references.
"""

import base64
import io
import logging
import os
import re
import subprocess
import tempfile
import wave

import httpx
import numpy as np
import openai

log = logging.getLogger(__name__)

# The transcriptions endpoint rejects uploads over 25 MB.
_API_MAX_BYTES = 25 * 1024 * 1024

# gpt-4o-transcribe-diarize rejects "audio duration … longer than 1400
# seconds"; margin because afinfo durations are estimates.
_MAX_SINGLE_SECONDS = 1300.0
# Server-side processing runs at roughly real time, so shorter chunks keep
# each request well inside the timeout; chunks after the first run in
# parallel, so more chunks barely cost wall-clock time.
_CHUNK_SECONDS = 600.0
_CHUNK_WORKERS = 3
_SPLIT_SEARCH_SECONDS = 15.0  # hunt for a quiet split point this far around the mark

_PCM_RATE = 16000  # decode rate for chunking; plenty for speech models

# Speaker reference clips must be 2-10s; at most 4 known speakers. Use
# nearly the full window — longer refs give the matcher a stronger
# voiceprint, and weak matches make later chunks mint spurious speakers.
_REF_MIN_SECONDS = 2.0
_REF_TARGET_SECONDS = 9.5
_MAX_REFS = 4

# Formats the API accepts as-is.
_NATIVE_EXTENSIONS = {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "wav", "webm"}
# CoreAudio-readable formats afconvert re-encodes to .m4a before upload.
_CONVERT_EXTENSIONS = {"3gp", "aac", "aif", "aifc", "aiff", "alac", "amr", "caf", "m4b", "m4r"}

AUDIO_EXTENSIONS = _NATIVE_EXTENSIONS | _CONVERT_EXTENSIONS

# A long recording uploads slowly and is chunk-transcribed server-side; the
# interactive client's 30s budget is far too small here.
_TIMEOUT = httpx.Timeout(900.0, connect=10.0)


class _AudioTooLong(Exception):
    """Single-shot request rejected for duration; retry via chunking."""


def is_audio_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in AUDIO_EXTENSIONS and os.path.isfile(path)


def transcribe_file(client, path, model, fallback_model=None, on_progress=None,
                    language=None):
    """Transcribe one audio file. Returns (formatted_text, duration_seconds).

    fallback_model, if given, is retried plain (no diarization) when the API
    rejects the diarize model — e.g. an org without access to it.
    on_progress, if given, receives short human-readable stage strings on the
    calling thread.
    language ("de", "en", …) pins the transcription language. Auto-detection
    happens per server-side chunk and can drift — German speech has come back
    translated into English mid-file.
    """
    notify = on_progress or (lambda msg: None)
    duration = _probe_duration(path)
    if duration is not None and duration > _MAX_SINGLE_SECONDS:
        return _transcribe_chunked(client, path, model, fallback_model, notify, language)
    try:
        return _transcribe_single(client, path, model, fallback_model, notify, language)
    except _AudioTooLong:
        # afinfo under-reported (or was unparseable); do it the long way.
        log.info("%s too long for a single request; chunking", os.path.basename(path))
        return _transcribe_chunked(client, path, model, fallback_model, notify, language)


# ── API calls ─────────────────────────────────────────────────────────────


def _create(client, model, f, diarize, names=None, refs=None, language=None):
    kwargs = {}
    if language:
        kwargs["language"] = language
    if diarize:
        kwargs["response_format"] = "diarized_json"
        # Required for audio over 30s: the API splits long audio server-side
        # and keeps speaker identities across its internal chunks.
        kwargs["chunking_strategy"] = "auto"
        if names:
            kwargs["known_speaker_names"] = names
            kwargs["known_speaker_references"] = refs
    return client.audio.transcriptions.create(model=model, file=f, timeout=_TIMEOUT, **kwargs)


def _create_with_fallback(client, model, fallback_model, f, names=None, refs=None,
                          language=None):
    """Diarized call with plain-model retry. Returns (result, used_diarize)."""
    try:
        return _create(client, model, f, diarize=True, names=names, refs=refs,
                       language=language), True
    except (openai.NotFoundError, openai.BadRequestError) as e:
        if isinstance(e, openai.BadRequestError) and _is_too_long_error(e):
            raise _AudioTooLong() from e
        if not fallback_model or fallback_model == model:
            raise
        log.warning(
            "diarized transcription with %s rejected (%s); retrying plain with %s",
            model, e, fallback_model,
        )
        f.seek(0)
        return _create(client, fallback_model, f, diarize=False, language=language), False


def _is_too_long_error(e) -> bool:
    msg = str(e).lower()
    return "longer than" in msg or "input_too_large" in msg or "too large" in msg


def _transcribe_single(client, path, model, fallback_model, notify, language=None):
    upload_path, is_temp = _prepare_upload(path)
    try:
        notify("transcribing…")
        with open(upload_path, "rb") as f:
            result, _ = _create_with_fallback(client, model, fallback_model, f,
                                              language=language)
    finally:
        if is_temp:
            _unlink_quiet(upload_path)
    return _format_result(result), _result_duration(result)


# ── chunked path (long recordings) ────────────────────────────────────────


def _transcribe_chunked(client, path, model, fallback_model, notify, language=None):
    notify("preparing…")
    wav_path = _decode_to_wav(path)
    try:
        with wave.open(wav_path, "rb") as r:
            rate = r.getframerate()
            samples = np.frombuffer(r.readframes(r.getnframes()), dtype=np.int16)
    finally:
        _unlink_quiet(wav_path)

    total_duration = len(samples) / rate
    bounds = _chunk_bounds(samples, rate)
    total = len(bounds)
    log.info("chunking %s: %.0fs into %d parts", os.path.basename(path), total_duration, total)

    # Chunk 1 runs alone: its diarized segments yield the speaker reference
    # clips that keep labels consistent everywhere else.
    notify(f"part 1/{total}…")
    a, b = bounds[0]
    result, diarize = _transcribe_chunk(client, model, fallback_model, samples[a:b],
                                        rate, language=language)
    results: list = [result]

    # Chunk 1's raw labels are mapped once and reused at stitch time below:
    # they are chunk-local letters, not registry names, so a second
    # map_chunk() call would mint fresh ids and split every speaker in two.
    speakers = _SpeakerRegistry()
    local1 = None
    if diarize:
        local1 = speakers.map_chunk(result)
        speakers.collect_refs(result, local1, samples[a:b], rate)

    # Remaining chunks only need those refs, so they can run in parallel —
    # server-side processing is roughly real time, which sequential chunking
    # would compound into multiples of the recording length.
    if total > 1:
        notify(f"parts 2-{total}/{total}…" if total > 2 else f"part 2/2…")
        import concurrent.futures as cf

        done = 1

        def run(idx):
            ca, cb = bounds[idx]
            r, _ = _transcribe_chunk(
                client, model if diarize else fallback_model,
                fallback_model, samples[ca:cb], rate,
                names=speakers.names or None, refs=speakers.refs or None,
                diarize=diarize, language=language,
            )
            return r

        with cf.ThreadPoolExecutor(max_workers=_CHUNK_WORKERS) as ex:
            futures = {ex.submit(run, idx): idx for idx in range(1, total)}
            results += [None] * (total - 1)
            for fut in cf.as_completed(futures):
                results[futures[fut]] = fut.result()
                done += 1
                if done < total:
                    notify(f"{done}/{total} parts done…")

    turns: list[tuple[int | None, str]] = []  # (canonical speaker id | None, text)
    for idx, result in enumerate(results):
        segments = getattr(result, "segments", None) or []
        if segments:
            local = local1 if idx == 0 and local1 is not None else speakers.map_chunk(result)
            for seg in segments:
                seg_text = (seg.text or "").strip()
                if seg_text:
                    turns.append((local[seg.speaker], seg_text))
        else:
            plain = (result.text or "").strip()
            if plain:
                turns.append((None, plain))

    return _render_turns(turns), total_duration


def _transcribe_chunk(client, model, fallback_model, chunk_samples, rate,
                      names=None, refs=None, diarize=True, language=None):
    chunk_path = _encode_chunk(chunk_samples, rate)
    try:
        with open(chunk_path, "rb") as f:
            if diarize:
                return _create_with_fallback(
                    client, model, fallback_model, f, names=names, refs=refs,
                    language=language,
                )
            return _create(client, model, f, diarize=False, language=language), False
    finally:
        _unlink_quiet(chunk_path)


class _SpeakerRegistry:
    """Canonical speaker identities across chunks.

    Chunk 1 speakers get reference clips extracted from their diarized
    segments; later chunks receive them via known_speaker_names ("S1"…) so
    the API maps the same voice back to the same identity. Labels the API
    invents for new speakers (letters) are chunk-local and must not be
    carried across chunks.
    """

    def __init__(self):
        self.names: list[str] = []  # e.g. ["S1", "S2"] — sent to the API
        self.refs: list[str] = []  # matching data-URL reference clips
        self._known: dict[str, int] = {}  # API name -> canonical id
        self._count = 0

    def map_chunk(self, result) -> dict[str, int]:
        """Map this chunk's raw labels to canonical ids."""
        local: dict[str, int] = {}
        for seg in getattr(result, "segments", None) or []:
            label = seg.speaker
            if label in local:
                continue
            if label in self._known:
                local[label] = self._known[label]
            else:
                local[label] = self._count
                self._count += 1
        return local

    def collect_refs(self, result, local, chunk_samples, rate):
        """Extract reference clips for speakers that don't have one yet."""
        segments = getattr(result, "segments", None) or []
        have = set(self._known.values())
        for label, cid in local.items():
            if cid in have or len(self.names) >= _MAX_REFS:
                continue
            uri = _speaker_clip_uri(chunk_samples, rate,
                                    [s for s in segments if s.speaker == label])
            if uri is None:
                log.info("no usable reference clip for speaker %s; label may drift", label)
                continue
            name = f"S{cid + 1}"
            self.names.append(name)
            self.refs.append(uri)
            self._known[name] = cid


def _speaker_clip_uri(samples, rate, segs) -> str | None:
    """A 2-10s mono wav of this speaker's longest segment, as a data URL."""
    if not segs:
        return None
    best = max(segs, key=lambda s: (s.end or 0.0) - (s.start or 0.0))
    start = int((best.start or 0.0) * rate)
    end = int(min(best.end or 0.0, (best.start or 0.0) + _REF_TARGET_SECONDS) * rate)
    if end - start < int(_REF_MIN_SECONDS * rate):
        # Too short on its own: pad past the segment (may graze the next
        # speaker; the reference just needs to be dominated by this voice).
        end = min(len(samples), start + int(_REF_MIN_SECONDS * rate * 1.2))
    if end - start < int(_REF_MIN_SECONDS * rate):
        return None
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples[start:end].tobytes())
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _chunk_bounds(samples, rate) -> list[tuple[int, int]]:
    """Split points as (start, end) sample indices, cut at quiet moments."""
    chunk = int(_CHUNK_SECONDS * rate)
    slack = int(60 * rate)  # absorb a short tail into the last chunk
    bounds = []
    start, n = 0, len(samples)
    while n - start > chunk + slack:
        cut = _quiet_point(samples, start + chunk, rate)
        bounds.append((start, cut))
        start = cut
    bounds.append((start, n))
    return bounds


def _quiet_point(samples, target, rate) -> int:
    """The quietest 0.5s window within ±_SPLIT_SEARCH_SECONDS of target."""
    search = int(_SPLIT_SEARCH_SECONDS * rate)
    lo = max(0, target - search)
    hi = min(len(samples), target + search)
    win = int(0.5 * rate)
    hop = int(0.1 * rate)
    region = samples[lo:hi].astype(np.float64) ** 2
    if len(region) <= win:
        return target
    cumsum = np.concatenate(([0.0], np.cumsum(region)))
    starts = np.arange(0, len(region) - win, hop)
    energy = cumsum[starts + win] - cumsum[starts]
    best = starts[int(np.argmin(energy))]
    return lo + int(best) + win // 2


def _decode_to_wav(path) -> str:
    """Decode any CoreAudio-readable file to 16 kHz mono 16-bit wav."""
    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="mumbletype-")
    os.close(fd)
    cmd = ["afconvert", "-f", "WAVE", "-d", f"LEI16@{_PCM_RATE}", "-c", "1", "--mix", path, tmp]
    _run_afconvert(cmd, path, tmp)
    return tmp


def _encode_chunk(samples, rate) -> str:
    """Write a chunk of PCM to a temp .m4a (32 kbps mono AAC) for upload."""
    fd, wav_tmp = tempfile.mkstemp(suffix=".wav", prefix="mumbletype-chunk-")
    os.close(fd)
    fd, m4a_tmp = tempfile.mkstemp(suffix=".m4a", prefix="mumbletype-chunk-")
    os.close(fd)
    try:
        with wave.open(wav_tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(samples.tobytes())
        _run_afconvert(
            ["afconvert", "-f", "m4af", "-d", "aac", "-b", "32000", wav_tmp, m4a_tmp],
            wav_tmp, m4a_tmp,
        )
    finally:
        _unlink_quiet(wav_tmp)
    return m4a_tmp


def _probe_duration(path) -> float | None:
    try:
        out = subprocess.run(
            ["afinfo", path], capture_output=True, timeout=60
        ).stdout.decode(errors="replace")
        m = re.search(r"estimated duration:\s*([0-9.]+)", out)
        return float(m.group(1)) if m else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


# ── upload preparation (single-shot path) ─────────────────────────────────


def _prepare_upload(path: str) -> tuple[str, bool]:
    """Return (path_to_upload, is_temp_file). Re-encodes when the file is in
    a format the API doesn't take or over its size cap."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext in _NATIVE_EXTENSIONS and os.path.getsize(path) <= _API_MAX_BYTES:
        return path, False
    return _reencode(path), True


def _reencode(path: str) -> str:
    """Re-encode to 22.05 kHz mono 32 kbps AAC (~14 MB/hour) via the
    system's afconvert; plenty of fidelity for speech."""
    fd, tmp = tempfile.mkstemp(suffix=".m4a", prefix="mumbletype-")
    os.close(fd)
    cmd = ["afconvert", "-f", "m4af", "-d", "aac@22050", "-c", "1", "-b", "32000", path, tmp]
    _run_afconvert(cmd, path, tmp)
    if os.path.getsize(tmp) > _API_MAX_BYTES:
        _unlink_quiet(tmp)
        raise ValueError(f"{os.path.basename(path)} is too long even after re-encoding (25 MB API cap)")
    return tmp


def _run_afconvert(cmd, src, dst):
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as e:
        _unlink_quiet(dst)
        detail = (getattr(e, "stderr", b"") or b"").decode(errors="replace").strip()
        raise RuntimeError(f"could not convert {os.path.basename(src)}: {detail or e}") from e


def _unlink_quiet(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


# ── output formatting ─────────────────────────────────────────────────────


def _result_duration(result) -> float:
    """Audio length for usage tracking. The diarize endpoint has been seen
    returning duration=0, so fall back to the last segment's end time."""
    duration = float(getattr(result, "duration", 0.0) or 0.0)
    if duration > 0.0:
        return duration
    segments = getattr(result, "segments", None) or []
    return max((float(getattr(s, "end", 0.0) or 0.0) for s in segments), default=0.0)


def _speaker_display(cid: int) -> str:
    return chr(ord("A") + cid) if cid < 26 else str(cid + 1)


def _render_turns(turns) -> str:
    """Merge consecutive same-speaker turns; collapse to plain text when the
    whole recording has at most one speaker."""
    merged: list[tuple[int | None, list[str]]] = []
    for cid, text in turns:
        if merged and merged[-1][0] == cid:
            merged[-1][1].append(text)
        else:
            merged.append((cid, [text]))
    distinct = {cid for cid, _ in merged if cid is not None}
    if len(distinct) <= 1:
        return "\n\n".join(" ".join(parts) for _, parts in merged).strip()
    out = []
    for cid, parts in merged:
        body = " ".join(parts)
        out.append(body if cid is None else f"Speaker {_speaker_display(cid)}: {body}")
    return "\n\n".join(out)


def _format_result(result) -> str:
    """Render a single-shot diarized result as speaker turns; anything
    without at least two speakers collapses to plain text."""
    segments = getattr(result, "segments", None) or []
    speakers = {s.speaker for s in segments if getattr(s, "speaker", None)}
    if len(speakers) <= 1:
        return (result.text or "").strip()

    # The API has been seen emitting stray labels like "@"; we never pass
    # known_speaker_names here, so relabel by order of first appearance.
    labels: dict[str, int] = {}
    turns: list[tuple[int, str]] = []
    for seg in segments:
        seg_text = (seg.text or "").strip()
        if not seg_text:
            continue
        if seg.speaker not in labels:
            labels[seg.speaker] = len(labels)
        turns.append((labels[seg.speaker], seg_text))
    return _render_turns(turns)
