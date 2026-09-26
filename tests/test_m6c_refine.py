"""M6c refine-workspace tests: persisted subtitle params, structured preview,
edited-entry render + edit records."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from mai2srt.config import (
    Config, last_mai_json, set_last_mai_json, set_subtitle_params,
    subtitle_param_defaults, subtitle_params,
)
from mai2srt.server.app import create_app


def _cfg(tmp_path):
    return Config(data_dir=tmp_path)


def _client(tmp_path):
    return TestClient(create_app(_cfg(tmp_path)))


def _write_mai_json(tmp_path: Path, name: str = "sample.mai.json") -> Path:
    """Small two-speaker transcript: 8 words across 6 seconds."""
    words = [
        {"text": "こんにちは", "start": 0.0, "end": 0.8, "speaker": "1"},
        {"text": "皆さん", "start": 0.9, "end": 1.4, "speaker": "1"},
        {"text": "今日は", "start": 1.5, "end": 2.0, "speaker": "1"},
        {"text": "はい", "start": 2.1, "end": 2.4, "speaker": "2"},
        {"text": "よろしく", "start": 2.5, "end": 3.1, "speaker": "2"},
        {"text": "お願いします", "start": 3.2, "end": 4.2, "speaker": "2"},
        {"text": "では", "start": 5.0, "end": 5.3, "speaker": "1"},
        {"text": "始めましょう", "start": 5.4, "end": 6.2, "speaker": "1"},
    ]
    doc = {"text": "", "language": "ja", "duration": 6.5,
           "engine": "test", "words": words, "utterances": []}
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- params

def test_subtitle_params_defaults_and_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    assert subtitle_params(cfg) == subtitle_param_defaults()
    eff = set_subtitle_params(cfg, {"max_duration": 8.0, "max_chars": 42})
    assert eff["max_duration"] == 8.0 and eff["max_chars"] == 42.0
    # persisted
    assert subtitle_params(cfg)["max_duration"] == 8.0
    # untouched keys keep defaults
    assert subtitle_params(cfg)["expand"] == 0.25


def test_subtitle_params_validation(tmp_path):
    cfg = _cfg(tmp_path)
    eff = set_subtitle_params(cfg, {
        "max_duration": 999.0,          # out of range -> ignored
        "max_chars": "wide",            # non-numeric -> ignored
        "evil_key": 1,                  # unknown -> dropped
        "min_duration": True,           # bool is not a number -> ignored
    })
    assert eff["max_duration"] == 12.0
    assert eff["max_chars"] == 60.0
    assert eff["min_duration"] == 1.2
    assert "evil_key" not in eff


def test_last_mai_json_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    assert last_mai_json(cfg) is None
    set_last_mai_json(cfg, tmp_path / "a.mai.json")
    assert last_mai_json(cfg).endswith("a.mai.json")


def test_params_endpoint_roundtrip(tmp_path):
    c = _client(tmp_path)
    r = c.get("/api/params")
    assert r.status_code == 200
    assert r.json()["params"]["max_duration"] == 12.0
    assert r.json()["last_mai_json"] is None
    r = c.put("/api/params", json={"params": {"max_chars": 80}})
    assert r.status_code == 200
    assert r.json()["params"]["max_chars"] == 80.0
    assert c.get("/api/params").json()["params"]["max_chars"] == 80.0


def test_params_hides_missing_last_mai_json(tmp_path):
    """A deleted/moved mai.json must not be handed to the UI for auto-load:
    the refine page would otherwise pop a raw 400 on every visit."""
    cfg = _cfg(tmp_path)
    c = _client(tmp_path)
    ghost = tmp_path / "ghost.mai.json"
    set_last_mai_json(cfg, ghost)

    assert last_mai_json(cfg) == str(ghost)            # pointer is kept
    assert c.get("/api/params").json()["last_mai_json"] is None   # not offered

    ghost.write_text("{}", encoding="utf-8")           # file comes back
    assert c.get("/api/params").json()["last_mai_json"] == str(ghost)


# ---------------------------------------------------------------- preview

def test_preview_returns_structured_entries(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    r = c.post("/api/preview", json={"mai_json_path": str(src),
                                     "use_llm": False, "params": {}})
    assert r.status_code == 200
    d = r.json()
    assert len(d["words"]) == 8
    assert d["words"][0]["text"] == "こんにちは"
    assert d["entries_count"] >= 2
    assert d["dialogue"] >= 1            # speaker 1/2 exchange clusters
    for e in d["entries"]:
        assert 0 <= e["w0"] <= e["w1"] < 8
        assert isinstance(e["text"], str) and e["text"]
        if e["dialogue"]:
            assert e["text"].startswith("- ")
    # no edit record yet
    assert "edit" not in d


def test_preview_missing_file_400(tmp_path):
    c = _client(tmp_path)
    r = c.post("/api/preview", json={"mai_json_path": str(tmp_path / "nope.json")})
    assert r.status_code == 400


def _write_long_mai_json(tmp_path: Path, name: str = "long.mai.json") -> Path:
    """One speaker, 24 words across ~46s with 1s gaps: over every limit, so
    the LLM splitter actually gets invoked (candidates exist)."""
    words = []
    t0 = 0.0
    for i in range(24):
        words.append({"text": "単語%d" % i, "start": t0, "end": t0 + 1.0,
                      "speaker": "1"})
        t0 += 2.0
    doc = {"text": "", "language": "ja", "duration": t0,
           "engine": "test", "words": words, "utterances": []}
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_preview_llm_unavailable_note(tmp_path):
    """use_llm requested but no endpoint configured -> explicit note."""
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    r = c.post("/api/preview", json={"mai_json_path": str(src),
                                     "use_llm": True, "params": {}})
    assert r.status_code == 200
    assert r.json()["llm_note"] == "unavailable"
    assert r.json()["entries_count"] >= 1          # deterministic results


def test_preview_llm_failure_note_with_fallback(tmp_path):
    """Endpoint unreachable -> note carries the error, entries are fallback."""
    cfg = _cfg(tmp_path)
    from mai2srt.config import save_app_config
    save_app_config(cfg, {"llm": {
        "api_key": "sk-x", "model": "m", "base_url": "http://127.0.0.1:1"}})
    c = TestClient(create_app(cfg))
    src = _write_long_mai_json(tmp_path)
    r = c.post("/api/preview", json={"mai_json_path": str(src),
                                     "use_llm": True, "params": {}})
    assert r.status_code == 200
    note = r.json()["llm_note"]
    assert note and note != "unavailable"
    assert r.json()["entries_count"] >= 2          # fallback split happened


def test_preview_llm_note_null_when_off(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    r = c.post("/api/preview", json={"mai_json_path": str(src),
                                     "use_llm": False, "params": {}})
    assert r.json()["llm_note"] is None


# ---------------------------------------------------------------- render

def test_render_writes_srt_and_edit_record(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    edited = [
        {"w0": 0, "w1": 2, "text": "こんにちは皆さん今日は", "dialogue": False},
        {"w0": 3, "w1": 5, "text": "- はいよろしく\n- お願いします", "dialogue": True},
        {"w0": 6, "w1": 7, "text": "では始めましょう", "dialogue": False},
    ]
    r = c.post("/api/render", json={"mai_json_path": str(src),
                                    "entries": edited, "params": {}})
    assert r.status_code == 200
    d = r.json()
    srt = Path(d["srt_path"])
    # no audio on disk -> json stem + refine suffix, next to the json
    assert srt.name == "sample_精修.srt"
    text = srt.read_text(encoding="utf-8")
    assert "3\n" in text or text.count("-->") == 3
    assert "- はいよろしく" in text
    # timestamps come from word refs: first entry starts at 0, last ends ~6.2+
    assert "00:00:00,000" in text
    # edit record persisted and picked up by the next preview
    edit = src.with_name("sample.mai.edit.json")
    assert edit.exists()
    payload = json.loads(edit.read_text(encoding="utf-8"))
    assert payload["entries"] == edited and payload["version"] == 1
    r2 = c.post("/api/preview", json={"mai_json_path": str(src),
                                      "use_llm": False, "params": {}})
    assert r2.json()["edit"]["entries"] == edited


def test_render_validates_ranges(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    bad = [{"w0": 0, "w1": 99, "text": "x", "dialogue": False}]
    r = c.post("/api/render", json={"mai_json_path": str(src), "entries": bad})
    assert r.status_code == 400
    r = c.post("/api/render", json={"mai_json_path": str(src), "entries": []})
    assert r.status_code == 400


def test_render_empty_text_falls_back_to_words(tmp_path):
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    r = c.post("/api/render", json={
        "mai_json_path": str(src),
        "entries": [{"w0": 6, "w1": 7, "text": "", "dialogue": False}],
    })
    assert r.status_code == 200
    srt = Path(r.json()["srt_path"]).read_text(encoding="utf-8")
    assert "では始めましょう" in srt        # rebuilt from word refs


# ------------------------------------------------- transcription hand-off

def test_entry_records_word_index_mapping():
    from mai2srt.server.app import entry_records
    from mai2srt.transcribe.parser import Word

    words = [Word(str(ch), i * 1.0, i * 1.0 + 0.5, "1")
             for i, ch in enumerate("abcdef")]

    class E:
        def __init__(self, ws, text, dlg=False):
            self.words, self.text, self.is_dialogue = ws, text, dlg

    recs = entry_records(words, [E(words[0:3], "abc"), E(words[3:6], "def", True)])
    assert recs == [
        {"w0": 0, "w1": 2, "text": "abc", "dialogue": False},
        {"w0": 3, "w1": 5, "text": "def", "dialogue": True},
    ]


def test_preview_passes_llm_origin_record(tmp_path):
    """A transcription-time origin=llm record round-trips through preview so
    the refine page can restore it without the manual-dirty conflict gate."""
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    src.with_name("sample.mai.edit.json").write_text(json.dumps({
        "version": 1, "source": src.name, "saved_at": "2026-09-23T12:00:00",
        "origin": "llm", "params": {},
        "entries": [{"w0": 0, "w1": 2, "text": "x", "dialogue": False}],
    }, ensure_ascii=False), encoding="utf-8")
    r = c.post("/api/preview", json={"mai_json_path": str(src),
                                     "use_llm": False, "params": {}})
    assert r.json()["edit"]["origin"] == "llm"


# ------------------------------------------------- snapshot-rework (M6g)

def test_render_record_params_come_from_request(tmp_path):
    """The edit record stores the params that SHAPED the entries (request
    overrides), not whatever the stored config held at save time."""
    c = _client(tmp_path)
    c.put("/api/params", json={"params": {"max_chars": 60}})
    src = _write_mai_json(tmp_path)
    r = c.post("/api/render", json={
        "mai_json_path": str(src), "params": {"max_chars": 40},
        "entries": [{"w0": 0, "w1": 2, "text": "x", "dialogue": False}],
    })
    assert r.status_code == 200
    payload = json.loads(
        src.with_name("sample.mai.edit.json").read_text(encoding="utf-8"))
    assert payload["params"]["max_chars"] == 40.0


def test_edit_record_endpoint_manual_archive(tmp_path):
    """Record-only archive (refine file-switch auto-save): no .srt side
    effect, origin=manual, bad ranges rejected."""
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    entries = [{"w0": 0, "w1": 1, "text": "ab", "dialogue": False}]
    r = c.post("/api/edit_record", json={"mai_json_path": str(src),
                                         "entries": entries})
    assert r.status_code == 200
    payload = json.loads(
        src.with_name("sample.mai.edit.json").read_text(encoding="utf-8"))
    assert payload["origin"] == "manual" and payload["entries"] == entries
    assert not src.with_name("sample.mai.srt").exists()
    # bad ranges / empty list rejected
    r2 = c.post("/api/edit_record", json={
        "mai_json_path": str(src),
        "entries": [{"w0": 5, "w1": 2, "text": "x", "dialogue": False}]})
    assert r2.status_code == 400
    r3 = c.post("/api/edit_record", json={"mai_json_path": str(src),
                                          "entries": []})
    assert r3.status_code == 400
    r4 = c.post("/api/edit_record", json={
        "mai_json_path": str(tmp_path / "nope.json"), "entries": entries})
    assert r4.status_code == 400


def test_preview_llm_failure_does_not_archive(tmp_path):
    """llm_note != None (unavailable / failed) means the entries are the
    deterministic fallback -- they must NOT be archived as origin=llm."""
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    c.post("/api/preview", json={"mai_json_path": str(src),
                                 "use_llm": True, "params": {}})
    assert not src.with_name("sample.mai.edit.json").exists()


def test_preview_llm_success_archives_llm_record(tmp_path, monkeypatch):
    """A successful refine-run LLM split auto-archives exactly like a
    transcription-time one (origin=llm, request params)."""
    from types import SimpleNamespace

    import mai2srt.runner as runner

    def fake_build_entries(cfg, t, dur, segment_params=None,
                           post_params=None, use_llm=False, **kw):
        ws = sorted(t.words, key=lambda w: w.start)[:3]
        e = SimpleNamespace(words=ws, text="".join(w.text for w in ws),
                            is_dialogue=False)
        return [e], None          # llm_note None = success

    monkeypatch.setattr(runner, "build_entries", fake_build_entries)
    c = _client(tmp_path)
    src = _write_mai_json(tmp_path)
    r = c.post("/api/preview", json={"mai_json_path": str(src),
                                     "use_llm": True,
                                     "params": {"max_chars": 40}})
    assert r.status_code == 200 and r.json()["llm_note"] is None
    payload = json.loads(
        src.with_name("sample.mai.edit.json").read_text(encoding="utf-8"))
    assert payload["origin"] == "llm"
    assert payload["entries"] == [
        {"w0": 0, "w1": 2, "text": "こんにちは皆さん今日は", "dialogue": False}]
    assert payload["params"]["max_chars"] == 40.0
