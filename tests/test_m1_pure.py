"""Unit tests for the pure M1 logic: chunk planning, bitrate, parser, stitch."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.config import (  # noqa: E402
    CHUNK_TARGET_SECONDS, COMPRESS_TARGET_BYTES, MAX_SINGLE_UPLOAD_SECONDS,
    MIN_CHUNK_S, MP3_BITRATE_MAX_BPS, MP3_BITRATE_MIN_BPS,
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


def test_plan_splits_into_balanced_pieces():
    """40 minutes is the shape the user asked for: two 20-minute pieces, not
    "fill 29 minutes, then dump the remaining 11"."""
    pieces = plan_chunks(40 * 60.0, []).pieces
    assert len(pieces) == 2
    assert pieces[0] == (0.0, 1200.0)
    assert pieces[1] == (1200.0, 2400.0)


def test_plan_prefers_the_pause_nearest_the_even_split():
    dur = 40 * 60.0
    # pause midpoints: one dead on the even split, one 40s early
    plan = plan_chunks(dur, [1199.0, 1160.0])
    assert plan.pieces[0][1] == 1199.0


def test_plan_keeps_a_pause_inside_the_snap_window_only():
    dur = 40 * 60.0
    # 600s is 10 minutes off the 1200s split -- outside the +/-15% window,
    # so it must be ignored rather than drag the cut 10 minutes early
    plan = plan_chunks(dur, [600.0])
    assert plan.pieces == [(0.0, 1200.0), (1200.0, dur)]


def test_plan_even_split_when_there_are_no_pauses():
    # 7000s needs 3 pieces at a 3480s cap; with no pauses they are dead even
    plan = plan_chunks(7000.0, [], target_s=3480.0)
    assert len(plan.pieces) == 3
    step = 7000.0 / 3
    for i, (s, e) in enumerate(plan.pieces):
        assert s == pytest.approx(i * step)
        assert e == pytest.approx((i + 1) * step)


# --- no stub tail (issue #5) --------------------------------------------------

def test_plan_never_leaves_a_stub_tail():
    """A source a few seconds past a 29-min multiple used to end with a 2-6s
    piece: the old planner filled to the cap and appended the residue, and a
    residue that short is the media's quiet tail. The server answers such a
    piece with HTTP 422 "No speech was found in this audio", which used to
    kill the whole run.

    5224s = 87 min 4s is the shape from the issue (4 chunks, the LAST one 4s
    and 0.1 MiB). Balanced splitting has no residue to place: the minimum
    piece is target/2 = 870s = 14.5 min."""
    dur = 5224.0
    pieces = plan_chunks(dur, []).pieces
    assert len(pieces) == 4
    assert min(e - s for s, e in pieces) >= CHUNK_TARGET_SECONDS / 2


def test_option_c_buys_room_to_cut_inside_pauses():
    """Exactly three caps' worth of audio leaves the even split no slack at
    all (the cut can only sit at 1740s), so the planner takes one extra piece
    -- and only because that turns the hard cuts into pause-aligned ones."""
    dur = 3 * CHUNK_TARGET_SECONDS
    pauses = [dur / 4, dur / 2, 3 * dur / 4]          # nowhere near 1740s
    assert len(plan_chunks(dur, pauses).pieces) == 4


def test_option_c_is_skipped_when_extra_pieces_buy_nothing():
    """No pauses at all: one more piece is one more upload for the same hard
    cuts, so the minimum count wins."""
    dur = 3 * CHUNK_TARGET_SECONDS
    pieces = plan_chunks(dur, []).pieces
    assert len(pieces) == 3
    assert all(abs(e - s - CHUNK_TARGET_SECONDS) < 1e-6 for s, e in pieces)


def test_plan_invariants_hold_for_any_length():
    """Contiguous coverage, nothing over the cap, nothing under the floor, and
    every piece within 30% of the even split (i.e. no residue)."""
    for dur in [x * 7.3 for x in range(1, 2000)]:
        pieces = plan_chunks(dur, []).pieces
        assert pieces[0][0] == 0.0
        assert pieces[-1][1] == dur
        for (_a, b), (c, _d) in zip(pieces, pieces[1:]):
            assert b == c, dur                     # no gap, no overlap
        ideal = dur / len(pieces)
        for a, b in pieces:
            assert b - a >= min(MIN_CHUNK_S, dur) - 1e-9, (dur, b - a)
            assert b - a <= CHUNK_TARGET_SECONDS + 1e-9, dur
            assert b - a <= MAX_SINGLE_UPLOAD_SECONDS, dur
            # the even split is the point: no piece is a residue
            assert b - a >= ideal * 0.7 - 1e-9, (dur, b - a, ideal)


# --- silence scan / prepare progress (2026-10-13 fixes) -----------------------

def test_quiet_scan_skips_video_decode(monkeypatch, tmp_path):
    """-vn: the silence scan must not decode a video's picture stream --
    a 69-min video burned 11.5 min here decoding video for nothing."""
    from mai2srt.media import chunk as chunk_mod

    argv: dict = {}

    class _R:
        returncode = 0
        stderr = "silence_start: 11.0\nsilence_end: 12.0 | silence_duration: 1.0"

    def fake_run_tool(cmd, **kw):
        argv["cmd"] = cmd
        return _R()

    monkeypatch.setattr(chunk_mod, "run_tool", fake_run_tool)
    monkeypatch.setattr(chunk_mod, "resolve_tool", lambda name: "ffmpeg")
    gaps = chunk_mod._scan_quiet(tmp_path / "x.mp4", -31.0, 0.3, 100.0)
    assert gaps == [(11.0, 12.0)]        # start AND end, not just the end
    assert "-vn" in argv["cmd"]
    assert any("silencedetect=noise=-31.0dB" in a for a in argv["cmd"])


def test_prepare_reports_progress(monkeypatch, tmp_path):
    """prepare() must narrate the analysis / plan / encodes: long videos sit in
    preparation for minutes and a silent UI looked wedged. The lines stay
    plain -- the numbers behind them go to the log, shown on demand."""
    from mai2srt.media import chunk as chunk_mod
    from mai2srt.media.probe import MediaInfo

    monkeypatch.setattr(chunk_mod, "detect_quiet", lambda src, dur: chunk_mod.QuietMap(
        gaps=[(1329.0, 1330.0), (2669.0, 2670.0)],
        peak_db=-1.0, threshold_db=-31.0, duration_s=dur))

    def fake_cut(src, dst, s, e):
        dst.write_bytes(b"x" * 2048)
        return dst

    monkeypatch.setattr(chunk_mod, "cut_chunk", fake_cut)

    info = MediaInfo(duration_s=4000.0, channels=2, sample_rate=44100,
                     codec="aac", ext="mp4", size_bytes=10**9)
    lines: list[str] = []
    out = chunk_mod.prepare(Path("lec.mp4"), info, tmp_path,
                            on_progress=lines.append)

    # 4000s -> 3 balanced pieces (1333s ideal), each cut inside a planted pause
    assert [off for _, off in out] == [0.0, 1329.5, 2669.5]
    assert any("正在分析音频断点" in m for m in lines)
    assert any("切块：3 段" in m for m in lines)
    assert any("编码第 1/3 段" in m for m in lines)
    assert any("第 3/3 段就绪" in m for m in lines)


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
    assert any("直接上传" in m for m in lines)


def test_prepare_refuses_a_signal_free_file(monkeypatch, tmp_path):
    """Nothing but silence: fail before uploading a single byte."""
    from mai2srt.media import chunk as chunk_mod
    from mai2srt.media.probe import MediaError, MediaInfo

    monkeypatch.setattr(chunk_mod, "detect_quiet", lambda src, dur: chunk_mod.QuietMap(
        peak_db=-91.0, duration_s=dur, no_signal=True))
    info = MediaInfo(duration_s=4000.0, channels=2, sample_rate=44100,
                     codec="aac", ext="mp4", size_bytes=10**9)
    with pytest.raises(MediaError, match="没有检测到人声"):
        chunk_mod.prepare(Path("silent.mp4"), info, tmp_path)


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
