"""Audio-file transcription with speaker diarization.

Files dropped on the menubar icon (or picked via the menu) are sent to
OpenAI's diarization-capable transcription model. Multi-speaker audio is
rendered as "Speaker A: …" turns; single-speaker audio as plain text.
"""

import logging
import os
import subprocess
import tempfile

import httpx
import openai

log = logging.getLogger(__name__)

# The transcriptions endpoint rejects uploads over 25 MB.
_API_MAX_BYTES = 25 * 1024 * 1024

# Formats the API accepts as-is.
_NATIVE_EXTENSIONS = {"flac", "m4a", "mp3", "mp4", "mpeg", "mpga", "oga", "ogg", "wav", "webm"}
# CoreAudio-readable formats afconvert re-encodes to .m4a before upload.
_CONVERT_EXTENSIONS = {"3gp", "aac", "aif", "aifc", "aiff", "alac", "amr", "caf", "m4b", "m4r"}

AUDIO_EXTENSIONS = _NATIVE_EXTENSIONS | _CONVERT_EXTENSIONS

# A long recording uploads slowly and is chunk-transcribed server-side; the
# interactive client's 30s budget is far too small here.
_TIMEOUT = httpx.Timeout(900.0, connect=10.0)


def is_audio_file(path: str) -> bool:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in AUDIO_EXTENSIONS and os.path.isfile(path)


def transcribe_file(client, path: str, model: str, fallback_model: str | None = None) -> tuple[str, float]:
    """Transcribe one audio file. Returns (formatted_text, duration_seconds).

    fallback_model, if given, is retried plain (no diarization) when the API
    rejects the diarize model — e.g. an org without access to it.
    """
    upload_path, is_temp = _prepare_upload(path)
    try:
        with open(upload_path, "rb") as f:
            try:
                result = client.audio.transcriptions.create(
                    model=model,
                    file=f,
                    response_format="diarized_json",
                    # Required for audio over 30s: the API splits long audio
                    # server-side and keeps speaker identities across chunks.
                    chunking_strategy="auto",
                    timeout=_TIMEOUT,
                )
            except (openai.NotFoundError, openai.BadRequestError) as e:
                if not fallback_model or fallback_model == model:
                    raise
                log.warning(
                    "diarized transcription with %s rejected (%s); retrying plain with %s",
                    model, e, fallback_model,
                )
                f.seek(0)
                result = client.audio.transcriptions.create(
                    model=fallback_model, file=f, timeout=_TIMEOUT
                )
    finally:
        if is_temp:
            try:
                os.unlink(upload_path)
            except OSError:
                pass

    return _format_result(result), _result_duration(result)


# ── upload preparation ────────────────────────────────────────────────────


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
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as e:
        _unlink_quiet(tmp)
        detail = getattr(e, "stderr", b"") or b""
        raise RuntimeError(f"could not re-encode {os.path.basename(path)}: {detail.decode(errors='replace').strip() or e}") from e
    if os.path.getsize(tmp) > _API_MAX_BYTES:
        _unlink_quiet(tmp)
        raise ValueError(f"{os.path.basename(path)} is too long even after re-encoding (25 MB API cap)")
    return tmp


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


def _format_result(result) -> str:
    """Render a diarized result as speaker turns; anything without at least
    two speakers collapses to plain text."""
    segments = getattr(result, "segments", None) or []
    speakers = {s.speaker for s in segments if getattr(s, "speaker", None)}
    if len(speakers) <= 1:
        return (result.text or "").strip()

    # The API has been seen emitting stray labels like "@"; we never pass
    # known_speaker_names, so relabel A, B, C… by order of first appearance.
    labels: dict[str, str] = {}
    turns: list[tuple[str, list[str]]] = []
    for seg in segments:
        seg_text = (seg.text or "").strip()
        if not seg_text:
            continue
        if seg.speaker not in labels:
            labels[seg.speaker] = chr(ord("A") + len(labels))
        speaker = labels[seg.speaker]
        if turns and turns[-1][0] == speaker:
            turns[-1][1].append(seg_text)
        else:
            turns.append((speaker, [seg_text]))
    return "\n\n".join(f"Speaker {speaker}: {' '.join(parts)}" for speaker, parts in turns)
