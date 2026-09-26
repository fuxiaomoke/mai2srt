"""M6h refine audio playback: range streaming, per-file audio binding,
preview audio resolution (override > transcription-time source)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from mai2srt.config import Config, audio_override, set_audio_override
from mai2srt.server.app import create_app


def _cfg(tmp_path):
    return Config(data_dir=tmp_path)


def _client(tmp_path):
    return TestClient(create_app(_cfg(tmp_path)))


def _write_audio(tmp_path: Path, name: str = "song.mp3",
                 size: int = 4096) -> Path:
    p = tmp_path / name
    p.write_bytes(bytes((i % 251 for i in range(size))))
    return p


def _write_mai_json(tmp_path: Path, source: str | None = None,
                    name: str = "sample.mai.json") -> Path:
    words = [
        {"text": "こんにちは", "start": 0.0, "end": 0.8, "speaker": "1"},
        {"text": "皆さん", "start": 0.9, "end": 1.4, "speaker": "1"},
        {"text": "はい", "start": 2.1, "end": 2.4, "speaker": "2"},
        {"text": "よろしく", "start": 2.5, "end": 3.1, "speaker": "2"},
    ]
    doc = {"text": "", "language": "ja", "duration": 3.2,
           "engine": "test", "words": words, "utterances": []}
    if source is not None:
        doc["source"] = source
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _preview(c: TestClient, src: Path) -> dict:
    r = c.post("/api/preview", json={"mai_json_path": str(src),
                                     "use_llm": False, "params": {}})
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------- streaming

def test_audio_full_get(tmp_path):
    c = _client(tmp_path)
    a = _write_audio(tmp_path)
    r = c.get("/api/audio", params={"path": str(a)})
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == a.read_bytes()


def test_audio_range_requests(tmp_path):
    c = _client(tmp_path)
    a = _write_audio(tmp_path)
    data = a.read_bytes()

    r = c.get("/api/audio", params={"path": str(a)},
              headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 10-19/{len(data)}"
    assert r.headers["content-length"] == "10"
    assert r.content == data[10:20]

    # open-ended -> to EOF
    r = c.get("/api/audio", params={"path": str(a)},
              headers={"Range": "bytes=4090-"})
    assert r.status_code == 206
    assert r.content == data[4090:]

    # suffix form -> last N bytes
    r = c.get("/api/audio", params={"path": str(a)},
              headers={"Range": "bytes=-100"})
    assert r.status_code == 206
    assert r.content == data[-100:]

    # end clamped past EOF
    r = c.get("/api/audio", params={"path": str(a)},
              headers={"Range": f"bytes=0-{len(data) + 5000}"})
    assert r.status_code == 206
    assert r.content == data


def test_audio_range_rejections(tmp_path):
    c = _client(tmp_path)
    a = _write_audio(tmp_path)
    assert c.get("/api/audio", params={"path": str(a)},
                 headers={"Range": "bytes=abc"}).status_code == 416
    assert c.get("/api/audio", params={"path": str(a)},
                 headers={"Range": "bytes=-"}).status_code == 416
    assert c.get("/api/audio", params={"path": str(a)},
                 headers={"Range": "bytes=99999-100000"}).status_code == 416


def test_audio_path_validation(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/audio",
                 params={"path": str(tmp_path / "nope.mp3")}).status_code == 404
    bad = tmp_path / "notes.txt"
    bad.write_text("not audio")
    assert c.get("/api/audio", params={"path": str(bad)}).status_code == 415


# ---------------------------------------------------------------- binding

def test_config_audio_override_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    assert audio_override(cfg, "a.mai.json") is None
    set_audio_override(cfg, "a.mai.json", "x.mp3")
    assert audio_override(cfg, "a.mai.json") == "x.mp3"
    set_audio_override(cfg, "a.mai.json", None)
    assert audio_override(cfg, "a.mai.json") is None
    # the section itself disappears once the last binding is cleared
    assert "audio_overrides" not in json.loads(
        (tmp_path / "config.json").read_text(encoding="utf-8"))


def test_binding_endpoint_validation(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    a = _write_audio(tmp_path)
    # mai.json must exist
    assert c.put("/api/audio_binding", json={
        "mai_json_path": str(tmp_path / "nope.mai.json"),
        "audio_path": str(a)}).status_code == 400
    # audio must exist
    assert c.put("/api/audio_binding", json={
        "mai_json_path": str(src),
        "audio_path": str(tmp_path / "nope.mp3")}).status_code == 400
    # audio must be a supported type
    bad = tmp_path / "x.txt"
    bad.write_text("no")
    assert c.put("/api/audio_binding", json={
        "mai_json_path": str(src), "audio_path": str(bad)}).status_code == 400


def test_preview_audio_resolution_order(tmp_path):
    """override beats source; missing paths surface in `missing`."""
    c = _client(tmp_path)
    source_audio = _write_audio(tmp_path, "source.wav")
    other_audio = _write_audio(tmp_path, "vocal.mp3")
    src = _write_mai_json(tmp_path, source=str(source_audio))

    # source exists -> resolved to it, no override
    info = _preview(c, src)["audio"]
    assert info["resolved"] == str(source_audio)
    assert info["source"] == str(source_audio)
    assert info["override"] is None and info["missing"] is None
    assert info["duration_s"] == 3.2

    # bind a差分音轨 -> override wins
    r = c.put("/api/audio_binding",
              json={"mai_json_path": str(src), "audio_path": str(other_audio)})
    assert r.status_code == 200
    assert r.json()["audio"]["resolved"] == str(other_audio)
    info = _preview(c, src)["audio"]
    assert info["resolved"] == str(other_audio)
    assert info["override"] == str(other_audio)

    # override file vanishes -> falls back to the existing source
    other_audio.unlink()
    info = _preview(c, src)["audio"]
    assert info["resolved"] == str(source_audio)
    assert info["missing"] is None

    # both gone -> missing names the override (the would-be path)
    source_audio.unlink()
    info = _preview(c, src)["audio"]
    assert info["resolved"] is None
    assert info["missing"] == str(other_audio)

    # clearing the binding -> missing names the doc source
    c.put("/api/audio_binding",
          json={"mai_json_path": str(src), "audio_path": None})
    info = _preview(c, src)["audio"]
    assert info["override"] is None
    assert info["resolved"] is None
    assert info["missing"] == str(source_audio)


def test_preview_audio_no_source(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path, source=None)
    info = _preview(c, src)["audio"]
    assert info == {"source": None, "override": None, "resolved": None,
                    "pending": False, "pending_path": None,
                    "missing": None, "duration_s": 3.2}
