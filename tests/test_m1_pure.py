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
