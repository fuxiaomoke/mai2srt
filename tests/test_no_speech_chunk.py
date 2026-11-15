"""A speechless piece is an EMPTY result, not a fatal upload error (issue #5).

playground.microsoft.ai answers an audio upload that contains no speech with
HTTP 422 {"error":"No speech was found in this audio. ..."}. A long source
chunked at 29 minutes used to end with a 2-6s residual piece that sat entirely
inside the media's TRAILING silence -- silencedetect never reports a silence
that runs to EOF -- so the run died on its own last chunk and threw away every
piece that had already transcribed fine.

These tests pin the new contract:

- the classifier fires only on the content-level 422 (a plain 422 still fails);
- the upload is NOT retried (silence will not turn into words);
- the piece contributes an empty transcript, so offsets stay in the timeline;
- the conversation is still deleted, even on the failure path (issue #5 found
  the 422 leaving an orphan conversation behind, breaking "leave no trace");
- an all-silent file fails with a human message instead of an empty .srt.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt import runner  # noqa: E402
from mai2srt.runner import _require_speech, _upload_pieces  # noqa: E402
from mai2srt.transcribe import client  # noqa: E402
from mai2srt.transcribe.client import (  # noqa: E402
    TranscribeError, UploadResult, _fatal_status_codes, _upload_once,
    is_no_speech_rejection,
)
from mai2srt.transcribe.parser import parse_rich  # noqa: E402
from mai2srt.transcribe.stitch import stitch  # noqa: E402

#: the exact body from the issue report
NO_SPEECH_BODY = json.dumps({
    "error": ("No speech was found in this audio. Check the recording isn't "
              "silent, and that any music or background noise isn't drowning "
              "out the voices.")})
SILENT_MSG = f"stream HTTP 422: {NO_SPEECH_BODY}"


def _rich(text: str = "你好", start: float = 0.0, end: float = 1.0) -> dict:
    return {"durationSec": 1740.0, "utterances": [{
        "speaker": 1, "start": start, "end": end, "text": text,
        "language": "zh",
        "words": [{"text": text, "start": start, "end": end}],
    }]}


# --------------------------------------------------------------- detection

def test_classifier_matches_the_reported_body():
    assert is_no_speech_rejection(SILENT_MSG) is True


def test_classifier_matches_the_add_step_shape():
    # the body is the marker, not the step it came from
    assert is_no_speech_rejection(
        f'stage=add create=200 add=422 add_body={NO_SPEECH_BODY}') is True


def test_classifier_ignores_unrelated_failures():
    assert is_no_speech_rejection("stream HTTP 500: boom") is False
    assert is_no_speech_rejection(
        "stage=add add=413 add_body=Request body too large") is False
    assert is_no_speech_rejection("stream HTTP 401: expired") is False
    # a 422 WITHOUT the content-level body is a real request error
    assert is_no_speech_rejection(
        'stream HTTP 422: {"error":"unsupported codec"}') is False


def test_plain_422_stays_fatal():
    # only the speechless-shaped 422 gets its dedicated path: an unrelated one
    # is still a deterministic 4xx that must fail loudly instead of being
    # silently swallowed as "no speech"
    assert _fatal_status_codes(
        'stream HTTP 422: {"error":"unsupported codec"}') == [422]


# ------------------------------------------------- no retry, empty result

def test_silent_piece_returns_empty_result_without_retry(monkeypatch):
    calls = {"n": 0}

    async def fake_upload(*a, **k):
        calls["n"] += 1
        raise TranscribeError(SILENT_MSG)

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    monkeypatch.setattr(client, "RETRY_BACKOFF_S", (0, 0, 0))

    res = asyncio.run(client.transcribe_file(
        None, Path("x.mp3"), "audio/mpeg"))
    assert res.no_speech is True
    assert res.rich == {}                  # an empty transcription, not a crash
    assert calls["n"] == 1                 # silence never retries


def test_silent_piece_carries_the_cleanup_outcome(monkeypatch):
    async def fake_upload(*a, **k):
        e = TranscribeError(SILENT_MSG)
        e.cleanup_ok = False               # the delete failed too
        raise e

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    res = asyncio.run(client.transcribe_file(
        None, Path("x.mp3"), "audio/mpeg"))
    # the runner turns cleanup_ok=False into a user-facing warning
    assert res.no_speech is True
    assert res.cleanup_ok is False


# ------------------------------------------------- the real upload path

class _FakePage:
    """Just enough playwright Page for _upload_once: dispatch on the payload
    shape and record the conversation deletes."""

    def __init__(self, stream: dict):
        self.stream = stream
        self.deletes: list[str] = []

    async def evaluate(self, js, arg):
        if "createUrl" in arg:
            add = {"userMessage": {"id": "u1"},
                   "modelMessage": {"id": "m1"}}
            return {"stage": "ready", "convId": "conv-1",
                    "create": {"status": 200, "text": "{}"},
                    "add": {"status": 200,
                            "text": json.dumps({"result": {"data": {
                                "json": add}}})}}
        if "streamUrl" in arg:
            return self.stream
        if "url" in arg:                   # the delete
            self.deletes.append(arg["url"])
            return {"status": 200, "text": "{}"}
        raise AssertionError(f"unexpected evaluate: {str(js)[:60]}")


def _audio(tmp_path: Path) -> Path:
    p = tmp_path / "c.mp3"
    p.write_bytes(b"ID3" + b"\0" * 64)
    return p


def test_stream_422_still_deletes_the_conversation(tmp_path):
    """The issue's path created a playground conversation and then skipped the
    delete because the stream check raised first."""
    page = _FakePage({"status": 422, "sse": NO_SPEECH_BODY})
    with pytest.raises(TranscribeError) as ei:
        asyncio.run(_upload_once(page, _audio(tmp_path), "audio/mpeg",
                                 diarize=True, keep=False, title="t"))
    assert page.deletes == [f"{client.TRPC}/conversations.delete?batch=1"]
    assert ei.value.cleanup_ok is True


def test_silent_upload_end_to_end(tmp_path):
    page = _FakePage({"status": 422, "sse": NO_SPEECH_BODY})
    res = asyncio.run(client.transcribe_file(
        page, _audio(tmp_path), "audio/mpeg"))
    assert res.no_speech is True
    assert res.cleanup_ok is True
    assert page.deletes                    # trace left, nothing retained


def test_keep_still_skips_cleanup(tmp_path):
    page = _FakePage({"status": 422, "sse": NO_SPEECH_BODY})
    with pytest.raises(TranscribeError):
        asyncio.run(_upload_once(page, _audio(tmp_path), "audio/mpeg",
                                 diarize=True, keep=True, title="t"))
    assert page.deletes == []


# ------------------------------------------- the run survives a silent piece

def _silent_result() -> UploadResult:
    return UploadResult(rich={}, conversation_id="", request_id=None,
                        cleanup_ok=True, no_speech=True)


def _speech_result(text: str = "你好") -> UploadResult:
    return UploadResult(rich=_rich(text), conversation_id="c", request_id="r",
                        cleanup_ok=True)


def test_upload_pieces_skips_a_silent_piece_and_keeps_offsets(monkeypatch):
    pieces = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 1740.0),
              (Path("c.mp3"), 3480.0)]
    results = [_speech_result("开头"), _silent_result(), _speech_result("结尾")]
    seen: list[str] = []

    async def fake_upload(page, upath, mime, **k):
        seen.append(upath.name)
        return results[len(seen) - 1]

    monkeypatch.setattr(runner, "transcribe_file", fake_upload)
    logs: list[str] = []
    parts, raws, silent = asyncio.run(_upload_pieces(
        None, pieces, audio=Path("src.mp3"), diarize=True, keep=False,
        on_stage=lambda s, d: None, on_log=logs.append))

    assert seen == ["a.mp3", "b.mp3", "c.mp3"]   # a silent piece never aborts
    assert silent == [2]                         # 1-based, for the UI warning
    assert len(parts) == 3                       # offsets survive
    assert any("没有人声" in line for line in logs)

    stitched = stitch(parts, 5224.0)
    assert [w.text for w in stitched.transcript.words] == ["开头", "结尾"]
    # the third piece's word is shifted onto the source timeline
    assert stitched.transcript.words[1].start == 3480.0


def test_upload_pieces_still_aborts_on_a_wordless_piece(monkeypatch):
    """no_speech=False + no words stays a protocol regression, not silence."""
    async def fake_upload(page, upath, mime, **k):
        return UploadResult(rich={}, conversation_id="c", request_id="r",
                            cleanup_ok=True)

    monkeypatch.setattr(runner, "transcribe_file", fake_upload)
    with pytest.raises(TranscribeError, match="no word timestamps"):
        asyncio.run(_upload_pieces(
            None, [(Path("a.mp3"), 0.0)], audio=Path("src.mp3"),
            diarize=True, keep=False,
            on_stage=lambda s, d: None, on_log=lambda m: None))


def test_all_silent_file_fails_with_a_human_message():
    # every piece speechless -> no subtitle exists; an empty .srt would look
    # like success while delivering nothing
    stitched = stitch([(parse_rich({}), 0.0, "a.mp3")], 5224.0)
    logs: list[str] = []
    with pytest.raises(TranscribeError) as ei:
        _require_speech(stitched, logs.append)
    assert "没有检测到人声" in str(ei.value)
    assert logs                           # the same line lands in the job log


def test_require_speech_passes_when_any_piece_spoke():
    _require_speech(stitch([(parse_rich(_rich()), 0.0, "a.mp3")], 60.0),
                    lambda m: None)      # no raise
