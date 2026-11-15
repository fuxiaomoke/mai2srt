"""Seekable playback audio (2026-11-15): an MP3 is playable but not SEEKABLE.

Chromium's <audio> has no per-sample index to consult in an MP3, so a seek
pads the target time into a byte offset using the Xing header's 100-entry
TOC. On a VBR file that estimate is seconds off, and only the CLOCK is
right: currentTime reports the requested time while the decoder starts from
an earlier frame. Measured on the 55-min source that reported this (by
recording the element's own output and correlating it against the file):
seek 10:00 -> audible 09:55.7 (-4.3 s), seek 40:00 -> -12.1 s. Subtitle
Edit is unaffected because it scans frames.

So `.mp3` (and raw ADTS `.aac`) are routed through the extraction cache like
an unplayable source, and remuxed -- `-c:a copy`, bit-identical audio, 1.1 s
for an hour -- into an MP4 sample table named `.m4a`. The same frames then
seek exactly (measured 0.0 s error).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from mai2srt.config import Config, set_projects_dir
from mai2srt.media import playback
from mai2srt.media.probe import probe
from mai2srt.server.app import create_app

ffmpeg = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(
    ffmpeg is None, reason="ffmpeg not on PATH (fixtures need it)")


def _cfg(tmp_path) -> Config:
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    set_projects_dir(cfg, tmp_path / "library")
    return cfg


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(_cfg(tmp_path)))


def _mp3(dst: Path, seconds: int = 2) -> Path:
    """A real mp3 fixture (sine tone, LAME + Xing header like a download)."""
    r = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libmp3lame", "-b:a", "128k", str(dst)],
        capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return dst


def _head(p: Path, n: int = 4096) -> bytes:
    with p.open("rb") as f:
        return f.read(n)


# ------------------------------------------------------------- the routing

def test_mp3_needs_a_cache_entry_but_stays_directly_bindable(tmp_path):
    """mp3 must go through the cache (seek), yet stay in the "directly
    playable" set that /api/audio_binding validates against."""
    song = _mp3(tmp_path / "song.mp3")
    assert playback.needs_extraction(song) is True
    assert ".mp3" in playback.DIRECT_PLAY_MIMES
    # formats the element both plays and seeks in are served untouched
    for name in ("song.m4a", "song.wav", "song.ogg", "song.flac"):
        assert playback.needs_extraction(tmp_path / name) is False


def test_mp3_is_remuxed_into_an_m4a_sample_table(tmp_path):
    cfg = _cfg(tmp_path)
    song = _mp3(tmp_path / "song.mp3")
    src_info = probe(song)

    out = playback.ensure_playable(cfg, song)
    assert out.suffix == ".m4a"
    assert out != song
    # the audio is copied, not re-encoded, and the timeline is preserved
    out_info = probe(out)
    assert out_info.codec == "mp3"
    assert out_info.duration_s == pytest.approx(src_info.duration_s, abs=0.15)
    # ... inside an MP4: 'ftyp' box, and 'moov' up front (+faststart) so a
    # ranged seek does not have to fetch the tail of the file first
    head = _head(out)
    assert head[4:8] == b"ftyp"
    assert b"moov" in head
    # second call is a cache hit, not a second remux
    assert playback.ensure_playable(cfg, song) == out
    assert playback.existing_cache(cfg, song) == out


def test_preview_reports_pending_until_the_remux_exists(tmp_path):
    cfg = _cfg(tmp_path)
    song = _mp3(tmp_path / "song.mp3")
    r = playback.resolve_playable(cfg, None, str(song))
    assert r["pending"] is True and r["resolved"] is None
    assert r["pending_path"] == str(song)
    playback.ensure_playable(cfg, song)
    r = playback.resolve_playable(cfg, None, str(song))
    assert r["pending"] is False
    assert str(r["resolved"]).endswith(".m4a")


def test_unprobeable_mp3_is_served_as_is(tmp_path):
    """A file that claims .mp3 but is not audio must not turn into a 415:
    the old approximate seek beats no playback at all."""
    cfg = _cfg(tmp_path)
    junk = tmp_path / "broken.mp3"
    junk.write_bytes(b"\0" * 4096)
    assert playback.ensure_playable(cfg, junk) == junk
    assert playback.existing_cache(cfg, junk) is None


# --------------------------------------------------------------- endpoint

def test_api_audio_serves_the_remuxed_mp4_for_an_mp3(tmp_path):
    c = _client(tmp_path)
    song = _mp3(tmp_path / "song.mp3")
    r = c.get("/api/audio", params={"path": str(song)})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content[4:8] == b"ftyp"        # the runtime serves the mp4 cache

    # ranges are cut out of the CACHE file, so the two must not be confused
    cache = sorted((tmp_path / "data" / "audio-cache").glob("*.m4a"))[0]
    r = c.get("/api/audio", params={"path": str(song)},
              headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{cache.stat().st_size}"
    assert r.content == _head(cache, 200)[100:200]
