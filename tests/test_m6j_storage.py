"""M6j projects storage: dedicated .mai.json library, SRT-follows-audio
exports, import/move/delete with pointer rebinding."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from mai2srt import storage
from mai2srt.config import (
    Config, audio_override, last_mai_json, projects_dir, set_audio_override,
    set_last_mai_json, set_projects_dir,
)
from mai2srt.server.app import create_app


def _cfg(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    set_projects_dir(cfg, tmp_path / "library")
    return cfg


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(_cfg(tmp_path)))


WORDS = [
    {"text": "こんにちは", "start": 0.0, "end": 0.8, "speaker": "1"},
    {"text": "皆さん", "start": 0.9, "end": 1.4, "speaker": "1"},
    {"text": "はい", "start": 2.1, "end": 2.4, "speaker": "2"},
    {"text": "よろしく", "start": 2.5, "end": 3.1, "speaker": "2"},
]


def _write_mai_json(p: Path, source: str | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"duration": 3.2, "language": "ja", "words": WORDS,
           "utterances": []}
    if source is not None:
        doc["source"] = source
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _write_audio(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * 2048)
    return p


def _entries() -> list[dict]:
    return [{"w0": 0, "w1": 1, "text": "こんにちは皆さん", "dialogue": False},
            {"w0": 2, "w1": 3, "text": "はいよろしく", "dialogue": True}]


# ------------------------------------------------------------ config + naming

def test_projects_dir_default_and_roundtrip(tmp_path):
    assert storage.default_projects_dir().name == "mai2srt"
    cfg = Config(data_dir=tmp_path)
    assert projects_dir(cfg) == storage.default_projects_dir()
    set_projects_dir(cfg, tmp_path / "lib")
    assert projects_dir(cfg) == tmp_path / "lib"


def test_sanitize_suffix_strips_hostile_chars():
    assert storage.sanitize_suffix(None) == "_精修"
    assert storage.sanitize_suffix("") == "_精修"
    assert storage.sanitize_suffix("_refined") == "_refined"
    # path separators / reserved chars cannot survive into a filename
    assert "/" not in storage.sanitize_suffix("../ev/il")
    assert "\\" not in storage.sanitize_suffix("a\\b:c*d")


def test_json_stem_and_paired_edit():
    p = Path("x/sample.mai.json")
    assert storage.json_stem(p) == "sample"
    assert storage.paired_edit_path(p).name == "sample.mai.edit.json"
    q = Path("x/plain.json")
    assert storage.json_stem(q) == "plain"
    assert storage.paired_edit_path(q).name == "plain.edit.json"


def test_unique_target_numbers_collisions(tmp_path):
    first = storage.unique_target(tmp_path, "a.mai.json")
    assert first.name == "a.mai.json"
    first.write_text("{}", encoding="utf-8")
    second = storage.unique_target(tmp_path, "a.mai.json")
    assert second.name == "a 2.mai.json"


# ---------------------------------------------------------------- srt target

def test_srt_target_follows_audio(tmp_path):
    cfg = _cfg(tmp_path)
    audio_dir = tmp_path / "desktop"
    audio_dir.mkdir()
    audio = _write_audio(audio_dir / "track.wav")
    src = _write_mai_json(projects_dir(cfg) / "track.mai.json",
                          source=str(audio))
    # initial result: plain name next to the audio
    out = storage.srt_target(cfg, src, str(audio))
    assert out == audio_dir / "track.srt"
    # refined result: suffixed sibling in the same folder
    out = storage.srt_target(cfg, src, str(audio), refined=True)
    assert out == audio_dir / "track_精修.srt"
    out = storage.srt_target(cfg, src, str(audio), refined=True,
                             suffix="_refined")
    assert out == audio_dir / "track_refined.srt"


def test_srt_target_override_beats_source(tmp_path):
    cfg = _cfg(tmp_path)
    a = _write_audio(tmp_path / "a" / "t.wav")
    b = _write_audio(tmp_path / "b" / "t.wav")
    src = _write_mai_json(projects_dir(cfg) / "t.mai.json", source=str(a))
    set_audio_override(cfg, src, b)
    assert storage.srt_target(cfg, src, str(a)) == b.with_name("t.srt")


def test_srt_target_audio_missing_falls_back_to_json(tmp_path):
    cfg = _cfg(tmp_path)
    src = _write_mai_json(projects_dir(cfg) / "sample.mai.json",
                          source=str(tmp_path / "gone.wav"))
    out = storage.srt_target(cfg, src, str(tmp_path / "gone.wav"))
    assert out == projects_dir(cfg) / "sample.srt"
    out = storage.srt_target(cfg, src, None, refined=True)
    assert out == projects_dir(cfg) / "sample_精修.srt"


# --------------------------------------------------------- transcribe target

def test_transcribe_target_same_audio_overwrites(tmp_path):
    cfg = _cfg(tmp_path)
    audio = _write_audio(tmp_path / "a" / "test.wav")
    p1 = storage.transcribe_target(cfg, audio)
    assert p1.name == "test.mai.json"
    _write_mai_json(p1, source=str(audio))
    # same audio again -> the same project (refresh in place)
    assert storage.transcribe_target(cfg, audio) == p1


def test_transcribe_target_different_audio_siblings(tmp_path):
    cfg = _cfg(tmp_path)
    a = _write_audio(tmp_path / "one" / "test.wav")
    b = _write_audio(tmp_path / "two" / "test.wav")
    p1 = storage.transcribe_target(cfg, a)
    _write_mai_json(p1, source=str(a))
    p2 = storage.transcribe_target(cfg, b)
    assert p2.name == "test 2.mai.json"


# ---------------------------------------------------------------- list/import

def test_storage_list_reports_projects(tmp_path):
    cfg = _cfg(tmp_path)
    audio = _write_audio(tmp_path / "a" / "ok.wav")
    d = storage.ensure_projects_dir(cfg)
    _write_mai_json(d / "ok.mai.json", source=str(audio))
    _write_mai_json(d / "broken.mai.json",
                    source=str(tmp_path / "a" / "gone.wav"))
    c = TestClient(create_app(cfg))
    r = c.get("/api/storage")
    assert r.status_code == 200
    data = r.json()
    assert data["dir"] == str(d)
    assert data["exists"] is True
    assert [p["name"] for p in data["projects"]] == ["broken.mai.json",
                                                     "ok.mai.json"]
    ok = data["projects"][1]
    assert ok["audio"]["resolved"] == str(audio)
    assert ok["has_edit"] is False
    broken = data["projects"][0]
    assert broken["audio"]["resolved"] is None
    assert broken["audio"]["missing"] == str(tmp_path / "a" / "gone.wav")
    assert data["total_bytes"] > 0


def test_import_copy_and_collision_suffix(tmp_path):
    cfg = _cfg(tmp_path)
    ext = tmp_path / "ext"
    ext.mkdir()
    src = _write_mai_json(ext / "a.mai.json")
    c = TestClient(create_app(cfg))
    r = c.post("/api/storage/import",
               json={"paths": [str(src)], "mode": "copy"})
    assert r.status_code == 200
    d = r.json()
    assert d["imported"] == 1 and d["skipped"] == []
    assert src.is_file()                                # copy keeps it
    # importing the same file again gets a numbered sibling
    r2 = c.post("/api/storage/import",
                json={"paths": [str(src)], "mode": "copy"})
    names = sorted(p["name"] for p in r2.json()["projects"])
    assert names == ["a 2.mai.json", "a.mai.json"]


def test_import_move_rebinds_and_brings_edit_record(tmp_path):
    cfg = _cfg(tmp_path)
    ext = tmp_path / "ext"
    ext.mkdir()
    audio = _write_audio(tmp_path / "a" / "t.wav")
    src = _write_mai_json(ext / "t.mai.json", source=str(audio))
    edit = storage.paired_edit_path(src)
    edit.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    set_audio_override(cfg, src, audio)
    set_last_mai_json(cfg, src)

    c = TestClient(create_app(cfg))
    r = c.post("/api/storage/import",
               json={"paths": [str(src)], "mode": "move"})
    assert r.status_code == 200 and r.json()["imported"] == 1
    new = projects_dir(cfg) / "t.mai.json"
    assert new.is_file() and not src.exists() and not edit.exists()
    assert storage.paired_edit_path(new).is_file()
    assert audio_override(cfg, new) == str(audio)
    assert audio_override(cfg, src) is None
    assert last_mai_json(cfg) == str(new)


def test_import_rejects_bad_mode_and_skips_insiders(tmp_path):
    cfg = _cfg(tmp_path)
    d = storage.ensure_projects_dir(cfg)
    inside = _write_mai_json(d / "in.mai.json")
    c = TestClient(create_app(cfg))
    r = c.post("/api/storage/import",
               json={"paths": [str(inside), str(tmp_path / "nope.json")],
                     "mode": "move"})
    assert r.status_code == 200
    assert r.json()["imported"] == 0
    assert r.json()["skipped"] == ["in.mai.json", "nope.json"]
    r = c.post("/api/storage/import", json={"paths": [str(inside)],
                                            "mode": "teleport"})
    assert r.status_code == 400


# -------------------------------------------------------------------- delete

def test_delete_project_and_refuse_outsiders(tmp_path):
    cfg = _cfg(tmp_path)
    audio = _write_audio(tmp_path / "a" / "t.wav")
    d = storage.ensure_projects_dir(cfg)
    p = _write_mai_json(d / "t.mai.json", source=str(audio))
    storage.paired_edit_path(p).write_text("{}", encoding="utf-8")
    set_audio_override(cfg, p, audio)
    outside = _write_mai_json(tmp_path / "ext.mai.json")

    c = TestClient(create_app(cfg))
    assert c.delete("/api/storage/project",
                    params={"path": str(outside)}).status_code == 400
    r = c.delete("/api/storage/project", params={"path": str(p)})
    assert r.status_code == 200 and r.json()["deleted"] == "t.mai.json"
    assert not p.exists() and not storage.paired_edit_path(p).exists()
    assert audio_override(cfg, p) is None
    assert c.delete("/api/storage/project",
                    params={"path": str(p)}).status_code == 404


# ----------------------------------------------------------------- dir move

def test_storage_dir_change_moves_and_rebinds(tmp_path):
    cfg = _cfg(tmp_path)
    audio = _write_audio(tmp_path / "a" / "t.wav")
    old = storage.ensure_projects_dir(cfg)
    p = _write_mai_json(old / "t.mai.json", source=str(audio))
    set_audio_override(cfg, p, audio)
    set_last_mai_json(cfg, p)

    new_dir = tmp_path / "library2"
    c = TestClient(create_app(cfg))
    r = c.put("/api/storage/dir", json={"dir": str(new_dir), "move": True})
    assert r.status_code == 200 and r.json()["moved"] == 1
    moved = new_dir / "t.mai.json"
    assert moved.is_file() and not p.exists()
    assert projects_dir(cfg) == new_dir
    assert audio_override(cfg, moved) == str(audio)
    assert last_mai_json(cfg) == str(moved)
    # same dir again -> rejected
    assert c.put("/api/storage/dir",
                 json={"dir": str(new_dir), "move": True}).status_code == 400


def test_storage_dir_switch_without_move(tmp_path):
    cfg = _cfg(tmp_path)
    old = storage.ensure_projects_dir(cfg)
    p = _write_mai_json(old / "t.mai.json")
    new_dir = tmp_path / "elsewhere"
    c = TestClient(create_app(cfg))
    r = c.put("/api/storage/dir", json={"dir": str(new_dir), "move": False})
    assert r.status_code == 200
    assert p.is_file()                                # untouched
    assert projects_dir(cfg) == new_dir
    assert r.json()["projects"] == []                 # new dir is empty


# ------------------------------------------------------------ render targets

def test_render_srt_follows_audio_with_suffix(tmp_path):
    cfg = _cfg(tmp_path)
    audio = _write_audio(tmp_path / "desk" / "中文测试.wav")
    src = _write_mai_json(projects_dir(cfg) / "中文测试.mai.json",
                          source=str(audio))
    c = TestClient(create_app(cfg))
    r = c.post("/api/render", json={"mai_json_path": str(src),
                                    "entries": _entries(), "params": {}})
    assert r.status_code == 200
    assert Path(r.json()["srt_path"]) == tmp_path / "desk" / "中文测试_精修.srt"
    r = c.post("/api/render", json={"mai_json_path": str(src),
                                    "entries": _entries(), "params": {},
                                    "name_suffix": "_refined"})
    assert Path(r.json()["srt_path"]) == tmp_path / "desk" / "中文测试_refined.srt"


def test_render_srt_no_audio_lands_next_to_json(tmp_path):
    cfg = _cfg(tmp_path)
    src = _write_mai_json(projects_dir(cfg) / "sample.mai.json")
    c = TestClient(create_app(cfg))
    r = c.post("/api/render", json={"mai_json_path": str(src),
                                    "entries": _entries(), "params": {}})
    assert r.status_code == 200
    assert Path(r.json()["srt_path"]) == projects_dir(cfg) / "sample_精修.srt"


# ----------------------------------------------------------------- open flow

def test_storage_open_sets_last_pointer(tmp_path):
    cfg = _cfg(tmp_path)
    d = storage.ensure_projects_dir(cfg)
    p = _write_mai_json(d / "t.mai.json")
    c = TestClient(create_app(cfg))
    r = c.post("/api/storage/open", json={"path": str(p)})
    assert r.status_code == 200
    assert c.get("/api/params").json()["last_mai_json"] == str(p)
    assert c.post("/api/storage/open",
                  json={"path": str(d / "gone.mai.json")}).status_code == 404
