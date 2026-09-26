"""M6k video support: drag-in transcription + extracted-audio playback.

Transcription of video already worked structurally (probe picks the
audio stream, encode_mp3 strips with -vn); this increment adds the
no-audio guard, the on-demand extraction cache, the prepare flow, video
binding, and the cache manager endpoints."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from mai2srt.config import Config, set_projects_dir
from mai2srt.media import playback
from mai2srt.media.probe import MediaError, probe
from mai2srt.server.app import create_app

ffmpeg = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(
    ffmpeg is None, reason="ffmpeg not on PATH (fixtures need it)")


def _cfg(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    set_projects_dir(cfg, tmp_path / "library")
    return cfg


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(_cfg(tmp_path)))


def _lavfi(args: list[str], dst: Path) -> Path:
    r = subprocess.run([ffmpeg, "-y", "-v", "error", *args, str(dst)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return dst


def _video_with_audio(dst: Path, acodec: str = "aac",
                      container: str = ".mp4") -> Path:
    return _lavfi(
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
         "-c:a", acodec, "-c:v", "mpeg4"],
        dst.with_suffix(container))


def _video_no_audio(dst: Path) -> Path:
    return _lavfi(
        ["-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
         "-c:v", "mpeg4"],
        dst.with_suffix(".avi"))


WORDS = [{"text": "こんにちは", "start": 0.0, "end": 0.8, "speaker": "1"},
         {"text": "皆さん", "start": 0.9, "end": 1.4, "speaker": "1"}]


def _write_mai_json(p: Path, source: str | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"duration": 1.5, "language": "ja", "words": WORDS,
           "utterances": []}
    if source is not None:
        doc["source"] = source
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


# ------------------------------------------------------------------- probe

def test_probe_rejects_trackless_video(tmp_path):
    v = _video_no_audio(tmp_path / "silent")
    with pytest.raises(MediaError, match="no audio stream"):
        probe(v)


def test_probe_reads_video_audio_stream(tmp_path):
    v = _video_with_audio(tmp_path / "clip")
    info = probe(v)
    assert info.codec == "aac"
    assert info.duration_s > 0


# --------------------------------------------------------------- extraction

def test_ensure_playable_remuxes_aac_to_m4a(tmp_path):
    cfg = _cfg(tmp_path)
    v = _video_with_audio(tmp_path / "clip")
    out = playback.ensure_playable(cfg, v)
    assert out.suffix == ".m4a"
    assert playback.existing_cache(cfg, v) == out
    # second call hits the cache (same path object, no re-extract)
    assert playback.ensure_playable(cfg, v) == out
    # audio files pass through untouched
    a = tmp_path / "song.mp3"
    a.write_bytes(b"\0" * 64)
    assert playback.ensure_playable(cfg, a) == a


def test_ensure_playable_transcodes_undecodable_codec(tmp_path):
    cfg = _cfg(tmp_path)
    # ac3: Chromium cannot decode it -> mp3 transcode fallback
    v = _video_with_audio(tmp_path / "movie", acodec="ac3",
                          container=".mkv")
    assert probe(v).codec == "ac3"
    out = playback.ensure_playable(cfg, v)
    assert out.suffix == ".mp3"


def test_cache_key_invalidates_on_mtime(tmp_path):
    cfg = _cfg(tmp_path)
    v = _video_with_audio(tmp_path / "clip")
    first = playback.ensure_playable(cfg, v)
    # touch the source -> new key -> re-extraction
    import os
    st = v.stat()
    os.utime(v, (st.st_atime, st.st_mtime + 10))
    second = playback.ensure_playable(cfg, v)
    assert second != first


def test_resolve_playable_pending_then_ready(tmp_path):
    cfg = _cfg(tmp_path)
    v = _video_with_audio(tmp_path / "clip")
    r = playback.resolve_playable(cfg, None, str(v))
    assert r["pending"] is True and r["resolved"] is None
    assert r["pending_path"] == str(v)
    playback.ensure_playable(cfg, v)
    r = playback.resolve_playable(cfg, None, str(v))
    assert r["pending"] is False and r["resolved"].endswith(".m4a")


# ---------------------------------------------------------------- endpoints

def test_api_audio_serves_video_via_cache(tmp_path):
    c = _client(tmp_path)
    v = _video_with_audio(tmp_path / "clip")
    r = c.get("/api/audio", params={"path": str(v)})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mp4"
    # range request against the cached file
    r2 = c.get("/api/audio", params={"path": str(v)},
               headers={"Range": "bytes=0-9"})
    assert r2.status_code == 206
    assert r2.headers["content-length"] == "10"


def test_prepare_endpoint_flips_pending_to_resolved(tmp_path):
    c = _client(tmp_path)
    cfg_dir = tmp_path / "data"
    v = _video_with_audio(tmp_path / "clip")
    src = _write_mai_json(tmp_path / "sample.mai.json", source=str(v))
    # preview: pending video source
    info = c.post("/api/preview", json={"mai_json_path": str(src),
                                        "use_llm": False,
                                        "params": {}}).json()["audio"]
    assert info["pending"] is True and info["resolved"] is None
    # prepare: extraction happens, info refreshes
    r = c.post("/api/audio/prepare", json={"mai_json_path": str(src)})
    assert r.status_code == 200
    info = r.json()["audio"]
    assert info["pending"] is False
    assert info["resolved"] and Path(info["resolved"]).suffix == ".m4a"


def test_binding_accepts_video(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path / "sample.mai.json")
    v = _video_with_audio(tmp_path / "clip")
    r = c.put("/api/audio_binding",
              json={"mai_json_path": str(src), "audio_path": str(v)})
    assert r.status_code == 200
    audio = r.json()["audio"]
    assert audio["pending"] is True      # bound, cache not built yet
    # .txt still rejected
    bad = tmp_path / "x.txt"
    bad.write_text("no")
    assert c.put("/api/audio_binding",
                 json={"mai_json_path": str(src),
                       "audio_path": str(bad)}).status_code == 400


# ------------------------------------------------------------- cache manager

def test_cache_list_delete_clear(tmp_path):
    cfg = _cfg(tmp_path)
    v1 = _video_with_audio(tmp_path / "one")
    v2 = _video_with_audio(tmp_path / "two", container=".mkv")
    # two sources: one remuxes (aac/mp4), one transcodes (mkv default
    # codec is aac too -- force ac3 for variety)
    _lavfi(["-f", "lavfi", "-i", "sine=frequency=880:duration=1",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=64x64:rate=10",
            "-c:a", "ac3", "-c:v", "mpeg4"], v2)
    p1 = playback.ensure_playable(cfg, v1)
    p2 = playback.ensure_playable(cfg, v2)

    c = TestClient(create_app(cfg))
    data = c.get("/api/audio_cache").json()
    names = {e["file"] for e in data["entries"]}
    assert {p1.name, p2.name} <= names
    assert data["total_bytes"] > 0
    # index maps hashes back to source paths
    by_file = {e["file"]: e for e in data["entries"]}
    assert by_file[p1.name]["source"] == str(v1)

    # batch delete one entry; path traversal attempts are ignored
    r = c.post("/api/audio_cache/delete",
               json={"files": [p1.name, "../config.json"]})
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert not p1.exists() and p2.exists()

    # clear all
    r = c.post("/api/audio_cache/clear")
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert not p2.exists()
    assert c.get("/api/audio_cache").json()["entries"] == []


def test_storage_list_marks_video_sources(tmp_path):
    cfg = _cfg(tmp_path)
    c = TestClient(create_app(cfg))
    v = _video_with_audio(tmp_path / "clip")
    from mai2srt.config import projects_dir
    _write_mai_json(projects_dir(cfg) / "clip.mai.json", source=str(v))
    entry = c.get("/api/storage").json()["projects"][0]
    assert entry["audio"]["video"] is True
    assert entry["audio"]["pending"] is True   # read-only, no extraction
