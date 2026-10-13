"""Unit tests for the pure M1 logic: chunk planning, bitrate, parser, stitch."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.config import (  # noqa: E402
    COMPRESS_TARGET_BYTES, MP3_BITRATE_MAX_BPS, MP3_BITRATE_MIN_BPS,
)
from mai2srt.media.chunk import plan_chunks  # noqa: E402
from mai2srt.media.compress import mp3_bitrate_bps  # noqa: E402
from mai2srt.transcribe.client import parse_sse  # noqa: E402
from mai2srt.transcribe.parser import parse_rich  # noqa: E402
from mai2srt.transcribe.stitch import stitch  # noqa: E402


# --- bitrate ---------------------------------------------------------------

def test_bitrate_clamped():
    # target over 1h -> under the floor -> clamped to min
    assert mp3_bitrate_bps(3600) == MP3_BITRATE_MIN_BPS
    # very short -> clamped to max
    assert mp3_bitrate_bps(60) == MP3_BITRATE_MAX_BPS
    # in between: derived from the (live) compression target
    b = mp3_bitrate_bps(1800)
    assert MP3_BITRATE_MIN_BPS < b <= MP3_BITRATE_MAX_BPS
    assert abs(b - int(COMPRESS_TARGET_BYTES * 8 / 1800)) <= 1


# --- chunk planning ----------------------------------------------------------

def test_plan_short_audio_single_piece():
    plan = plan_chunks(1200.0, [])
    assert plan.pieces == [(0.0, 1200.0)]
    assert plan.offsets == [0.0]


def test_plan_prefers_silence_at_boundary():
    dur = 7000.0
    silences = [1000.0, 3300.0, 3450.0, 6900.0]
    plan = plan_chunks(dur, silences, target_s=3480.0)
    # first cut: latest silence <= 3480 -> 3450 (not the hard 3480)
    assert plan.pieces[0] == (0.0, 3450.0)
    # second: latest silence <= 3450+3480=6930 -> 6900
    assert plan.pieces[1] == (3450.0, 6900.0)
    assert plan.pieces[2] == (6900.0, dur)
    assert plan.offsets == [0.0, 3450.0, 6900.0]


def test_plan_hard_cut_when_no_silence():
    plan = plan_chunks(7000.0, [], target_s=3480.0)
    assert plan.pieces == [(0.0, 3480.0), (3480.0, 6960.0), (6960.0, 7000.0)]


# --- silence scan / prepare progress (2026-10-13 fixes) -----------------------

def test_detect_silences_skips_video_decode(monkeypatch, tmp_path):
    """-vn: the silence scan must not decode a video's picture stream --
    a 69-min video burned 11.5 min here decoding video for nothing."""
    from mai2srt.media import chunk as chunk_mod

    argv: dict = {}

    class _R:
        returncode = 0
        stderr = "silence_end: 12.0 | silence_duration: 1.0"

    def fake_run_tool(cmd, **kw):
        argv["cmd"] = cmd
        return _R()

    monkeypatch.setattr(chunk_mod, "run_tool", fake_run_tool)
    monkeypatch.setattr(chunk_mod, "resolve_tool", lambda name: "ffmpeg")
    ends = chunk_mod.detect_silences(tmp_path / "x.mp4")
    assert ends == [12.0]
    assert "-vn" in argv["cmd"]


def test_prepare_reports_progress(monkeypatch, tmp_path):
    """prepare() must narrate the silence scan / plan / encodes: long
    videos sit in preparation for minutes and a silent UI looked wedged."""
    from mai2srt.media import chunk as chunk_mod
    from mai2srt.media.probe import MediaInfo

    monkeypatch.setattr(chunk_mod, "detect_silences", lambda src: [1730.0, 3470.0])

    def fake_cut(src, dst, s, e):
        dst.write_bytes(b"x" * 2048)
        return dst

    monkeypatch.setattr(chunk_mod, "cut_chunk", fake_cut)

    info = MediaInfo(duration_s=4000.0, channels=2, sample_rate=44100,
                     codec="aac", ext="mp4", size_bytes=10**9)
    lines: list[str] = []
    out = chunk_mod.prepare(Path("lec.mp4"), info, tmp_path,
                            on_progress=lines.append)

    # 4000s -> 3 pieces cut at the planted silences (1730s, 3470s)
    assert [off for _, off in out] == [0.0, 1730.0, 3470.0]
    assert any("scanning for silence points" in m for m in lines)
    assert any("planned 3 chunks" in m for m in lines)
    assert any("encoding chunk 1/3" in m for m in lines)
    assert any("chunk 3/3 ready" in m for m in lines)


def test_prepare_progress_passthrough(monkeypatch, tmp_path):
    """A file already within limits skips chunking and says so."""
    from mai2srt.media import chunk as chunk_mod
    from mai2srt.media.probe import MediaInfo

    info = MediaInfo(duration_s=960.0, channels=2, sample_rate=44100,
                     codec="mp3", ext="mp3", size_bytes=1024)
    lines: list[str] = []
    out = chunk_mod.prepare(Path("s.mp3"), info, tmp_path,
                            on_progress=lines.append)
    assert out == [(Path("s.mp3"), 0.0)]
    assert any("sending as-is" in m for m in lines)


# --- SSE parsing -------------------------------------------------------------

def test_parse_sse_rich_and_text():
    sse = (
        'event: request_id\ndata: {"requestId":"rid-1"}\n\n'
        'data: {"type":"text","delta":"he"}\n'
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n'
        'data: {"rich_transcription":{"durationSec":9.5,"utterances":[{"speaker":1,'
        '"start":0.0,"end":1.0,"text":"hi","words":[{"text":"hi","start":0.0,"end":0.5}]}]}}\n'
        'data: [DONE]\n'
    )
    p = parse_sse(sse)
    assert p["request_id"] == "rid-1"
    assert p["text"] == "hello"
    assert p["rich"]["durationSec"] == 9.5


def test_parse_sse_warning_event():
    p = parse_sse('event: warning\ndata: {"code":"x"}\n\n')
    assert p["warnings"] == ['{"code":"x"}']


# --- parser --------------------------------------------------------------------

def test_parse_rich_normalizes_and_skips_bad_words():
    rt = {"durationSec": 12.0, "utterances": [
        {"speaker": 1, "start": 0.0, "end": 2.0, "text": "a b", "language": "ja",
         "words": [{"text": "a", "start": 0.0, "end": 0.4},
                   {"text": "", "start": 0.4, "end": 0.5},
                   {"text": "b", "start": 0.5, "end": 1.0}]},
        {"speaker": None, "start": 5.0, "end": 6.0, "text": "c",
         "words": [{"text": "c", "start": 5.0, "end": 5.5}]},
    ]}
    t = parse_rich(rt)
    assert [w.text for w in t.words] == ["a", "b", "c"]
    assert t.words[0].speaker == "1"
    assert t.words[2].speaker is None
    assert t.language == "ja"
    assert len(t.utterances) == 2
    assert t.text() == "a b\nc"


# --- stitch ----------------------------------------------------------------------

def test_stitch_offsets():
    rt1 = {"durationSec": 10.0, "utterances": [
        {"speaker": 1, "start": 0.0, "end": 5.0, "text": "one",
         "words": [{"text": "one", "start": 0.5, "end": 1.0}]}]}
    rt2 = {"durationSec": 10.0, "utterances": [
        {"speaker": 1, "start": 0.0, "end": 4.0, "text": "two",
         "words": [{"text": "two", "start": 0.2, "end": 0.8}]}]}
    s = stitch([(parse_rich(rt1), 0.0, "a.mp3"),
                (parse_rich(rt2), 3480.0, "b.mp3")], audio_duration_s=6900.0)
    assert s.transcript.words[1].start == pytest.approx(3480.2)
    assert s.transcript.utterances[1].start == pytest.approx(3480.0)
    assert s.transcript.duration_s == 6900.0
    assert s.transcript.language == "ja" or s.transcript.language is None
    assert [c.offset_s for c in s.chunks] == [0.0, 3480.0]
