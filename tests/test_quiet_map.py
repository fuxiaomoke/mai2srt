"""The adaptive pause map: measure the material, then find its pauses.

A fixed -40 dBFS silence line silently failed on real recordings. A normalised
master keeps its pauses at -55 dBFS, while a recording with room tone bottoms
out at -38 dBFS; one fixed number misses one of them, and a scan that finds
ZERO pauses turns every cut into a hard cut -- which is where issue #5's
residual stub came from. The line is now derived from the measured peak, and a
scan that contradicts itself is re-run once before being distrusted.

These tests pin: the derived line, both re-scan directions, the distrust
fallback (never "the file is silent", always "cut evenly instead"), and the
no-signal shortcut.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.config import (  # noqa: E402
    SILENCE_DB_MAX, SILENCE_DB_MIN, SILENCE_PEAK_FLOOR_DB, SILENCE_REL_DB,
)
from mai2srt.media import chunk as chunk_mod  # noqa: E402
from mai2srt.media.chunk import QuietMap, detect_quiet  # noqa: E402


def _fake_material(monkeypatch, peak, scans):
    """peak: dBFS or None; scans: list of gap lists, handed out per scan."""
    monkeypatch.setattr(chunk_mod, "measure_peak_db", lambda src: peak)
    calls: list[float] = []

    def fake_scan(src, threshold_db, min_s, duration_s):
        calls.append(threshold_db)
        idx = min(len(calls) - 1, len(scans) - 1)
        return scans[idx]

    monkeypatch.setattr(chunk_mod, "_scan_quiet", fake_scan)
    return calls


# ------------------------------------------------------------ the derived line

def test_threshold_follows_the_peak(monkeypatch):
    # a normalised master: peak -1 dB -> the line sits 30 dB below it
    calls = _fake_material(monkeypatch, -1.0, [[(1.0, 2.0)]])
    q = detect_quiet(Path("x.wav"), 100.0)
    assert calls == [pytest.approx(-1.0 - SILENCE_REL_DB)]
    assert q.threshold_db == pytest.approx(-31.0)
    assert q.trusted is True
    assert q.retried is False


def test_noisy_recording_still_finds_pauses(monkeypatch):
    """The case the fixed -40 line missed: room tone at -38, so nothing ever
    crossed -40 and every cut became a hard cut."""
    calls = _fake_material(monkeypatch, -1.0, [[(5.0, 5.6), (9.0, 9.5)]])
    q = detect_quiet(Path("x.wav"), 20.0)
    assert q.gaps == [(5.0, 5.6), (9.0, 9.5)]
    assert calls[0] > -40.0                 # a HIGHER line than the old fixed one


def test_threshold_is_clamped(monkeypatch):
    calls = _fake_material(monkeypatch, -3.0, [[(1.0, 2.0)]])
    detect_quiet(Path("x.wav"), 100.0)
    (lo, hi) = (SILENCE_DB_MIN, SILENCE_DB_MAX)
    assert lo <= calls[0] <= hi


def test_unmeasurable_peak_falls_back_to_the_fixed_line(monkeypatch):
    calls = _fake_material(monkeypatch, None, [[(1.0, 2.0)]])
    q = detect_quiet(Path("x.wav"), 100.0)
    assert calls == [pytest.approx(chunk_mod.SILENCE_NOISE_DB)]
    assert q.trusted is True


# --------------------------------------------------------- self-check / retry

def test_zero_pauses_triggers_a_higher_line(monkeypatch):
    calls = _fake_material(monkeypatch, -1.0, [[], [(3.0, 4.0)]])
    q = detect_quiet(Path("x.wav"), 100.0)
    assert len(calls) == 2
    assert calls[1] > calls[0]              # looser: something must count
    assert q.gaps == [(3.0, 4.0)]
    assert q.retried is True and q.trusted is True


def test_absurd_coverage_triggers_a_lower_line(monkeypatch):
    """One loud bang drags the peak (and the line) down, so nearly everything
    looks quiet: the strict re-scan must bring the speech back."""
    monkeypatch.setattr(chunk_mod, "SILENCE_SANITY_COVERAGE", 0.5)
    calls = _fake_material(monkeypatch, 0.0, [[(0.0, 100.0)], [(10.0, 11.0)]])
    q = detect_quiet(Path("x.wav"), 100.0)
    assert len(calls) == 2
    assert calls[1] < calls[0]              # stricter
    assert q.gaps == [(10.0, 11.0)]
    assert q.trusted is True and q.retried is True


def test_both_scans_implausible_means_hard_cuts_not_silence(monkeypatch):
    """The noisy nightmare: nothing looks quiet, then everything does. The map
    is useless -- but that must never be read as "the file has no speech"."""
    calls = _fake_material(monkeypatch, -1.0, [[], [(0.0, 100.0)]])
    monkeypatch.setattr(chunk_mod, "SILENCE_SANITY_COVERAGE", 0.5)
    q = detect_quiet(Path("x.wav"), 100.0)
    assert len(calls) == 2
    assert q.gaps == []
    assert q.trusted is False               # -> evenly spaced hard cuts
    assert q.no_signal is False             # NOT "nothing to transcribe"


# ------------------------------------------------------------- no signal

def test_signal_free_file_is_flagged(monkeypatch):
    monkeypatch.setattr(chunk_mod, "measure_peak_db", lambda src: -91.0)
    called: list[float] = []
    monkeypatch.setattr(chunk_mod, "_scan_quiet",
                        lambda *a: called.append(1) or [])
    q = detect_quiet(Path("x.wav"), 100.0)
    assert q.no_signal is True
    assert q.peak_db <= SILENCE_PEAK_FLOOR_DB
    assert called == []                     # no scan needed at all


def test_a_quiet_but_real_recording_is_not_called_silent(monkeypatch):
    # -30 dBFS peak is quiet, but well above the "no signal" floor
    _fake_material(monkeypatch, -30.0, [[(2.0, 3.0)]])
    q = detect_quiet(Path("x.wav"), 100.0)
    assert q.no_signal is False
    assert q.gaps == [(2.0, 3.0)]


# ------------------------------------------------------------- the map itself

def test_cut_points_use_pause_midpoints():
    q = QuietMap(gaps=[(10.0, 14.0), (20.0, 20.3)], duration_s=100.0)
    # only the pause long enough to cut inside; the cut is its MIDDLE
    assert q.cut_points() == [12.0]


def test_quiet_seconds_clips_to_the_piece():
    q = QuietMap(gaps=[(0.0, 10.0), (50.0, 60.0)], duration_s=100.0)
    assert q.quiet_seconds(5.0, 55.0) == pytest.approx(10.0)


def test_intervals_pair_starts_with_ends():
    stderr = (
        "[silencedetect] silence_start: 3.0\n"
        "[silencedetect] silence_end: 5.0 | silence_duration: 2.0\n"
    )
    assert chunk_mod._intervals(stderr, 100.0) == [(3.0, 5.0)]


def test_intervals_close_a_silence_left_open_at_eof():
    # older ffmpeg builds may print the start of an EOF silence without an end
    stderr = "[silencedetect] silence_start: 90.0\n"
    assert chunk_mod._intervals(stderr, 100.0) == [(90.0, 100.0)]
