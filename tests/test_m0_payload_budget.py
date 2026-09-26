"""Upload size budgeting: the playground's 25 MiB gate is on the RAW
file (same as the site's own picker). History: a 2026-09-26 theory
budgeted for the base64 payload (x4/3) after an apparent hang, but a
live website test transcribed a 23 MiB file (~30.7 MiB base64) fine --
the hang was a cancelled-in-progress transcription, not a rejection.
Budgeting is back to raw bytes; the theory-independent hardening
(timeouts, idle watchdog, no-retry-on-fatal-4xx, pre-flight gate) stays,
and so does the real fix that chunking must trigger before the site's
3615s gate when the bitrate floor overflows the limit."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.config import (
    CHUNK_TARGET_SECONDS, COMPRESS_TARGET_BYTES, MAX_AUDIO_SECONDS,
    MAX_SINGLE_UPLOAD_SECONDS, MAX_UPLOAD_BYTES, MP3_BITRATE_MIN_BPS,
)
from mai2srt.transcribe import client
from mai2srt.transcribe.client import TranscribeError, _fatal_status_codes


# ------------------------------------------------------------- budget math

def test_budget_constants_coherent():
    # compression target sits under the raw limit with headroom
    assert COMPRESS_TARGET_BYTES < MAX_UPLOAD_BYTES
    # floor bitrate fit: the longest single-upload audio must stay legal
    assert MAX_SINGLE_UPLOAD_SECONDS * (MP3_BITRATE_MIN_BPS / 8) < MAX_UPLOAD_BYTES
    # ...while chunks (never longer than the target) fit too
    assert CHUNK_TARGET_SECONDS * (MP3_BITRATE_MIN_BPS / 8) < MAX_UPLOAD_BYTES


# ------------------------------------------------------- pre-flight refusal

def test_upload_once_refuses_oversized_raw(tmp_path, monkeypatch):
    audio = tmp_path / "big.mp3"
    audio.write_bytes(b"\0" * 65536)
    monkeypatch.setattr(client, "MAX_UPLOAD_BYTES", 1024)  # tiny ceiling

    async def run():
        # page is never touched: the guard fires before any evaluate
        return await client._upload_once(
            None, audio, "audio/mpeg",
            diarize=True, keep=True, title="t")

    try:
        asyncio.run(run())
        raised = None
    except TranscribeError as e:
        raised = e
    assert raised is not None
    assert "raw" in str(raised) and "upload limit" in str(raised)


# ---------------------------------------------------------- retry behaviour

def test_fatal_4xx_extraction():
    m = ("stage=add create=200 create_body=ok add=413 "
         "add_body=Request body too large")
    assert _fatal_status_codes(m) == [413]
    # transient / auth codes never count as fatal
    assert _fatal_status_codes("stage=add add=429 add_body=slow down") == []
    assert _fatal_status_codes("stage=create create=401") == []
    assert _fatal_status_codes("stream HTTP 500: boom") == []
    assert _fatal_status_codes("stream HTTP 413: nope") == [413]


def test_transcribe_file_short_circuits_on_413(monkeypatch):
    calls = {"n": 0}

    async def fake_upload(*a, **k):
        calls["n"] += 1
        raise TranscribeError(
            "stage=add create=200 add=413 add_body=Request body too large")

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    monkeypatch.setattr(client, "RETRY_BACKOFF_S", (0, 0, 0))

    try:
        asyncio.run(client.transcribe_file(None, Path("x.mp3"), "audio/mpeg"))
        raised = None
    except TranscribeError as e:
        raised = e
    assert raised is not None and "not retrying" in str(raised)
    assert calls["n"] == 1          # no blind re-uploads of a dead body


def test_transcribe_file_retries_transient(monkeypatch):
    calls = {"n": 0}

    async def fake_upload(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TranscribeError("stage=add create=200 add=0 add_body=timeout")
        return "ok"

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    monkeypatch.setattr(client, "RETRY_BACKOFF_S", (0, 0, 0))
    assert asyncio.run(
        client.transcribe_file(None, Path("x.mp3"), "audio/mpeg")) == "ok"
    assert calls["n"] == 3


# --------------------------------------------------------- chunk triggering

def test_chunk_trigger_below_site_duration_limit():
    # the floor-fit bound sits between the chunk target and the site gate:
    # audios too long for one floor-bitrate upload must chunk even though
    # they are still under the 3615s site limit
    assert CHUNK_TARGET_SECONDS < MAX_SINGLE_UPLOAD_SECONDS < MAX_AUDIO_SECONDS
    # 3300s: under the site gate, over the floor fit -> 2 pieces
    from mai2srt.media.chunk import plan_chunks
    assert len(plan_chunks(3300.0, []).pieces) == 2
    # 3000s: fits a single upload at the floor -> 1 piece
    assert len(plan_chunks(3000.0, []).pieces) == 1
