# -*- coding: utf-8 -*-
"""Pre-flight gate tests: a job that cannot work must be refused, naming what
is missing (and never refused for a reason that is actually fine)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt import preflight
from mai2srt.config import Config, add_account, cookie_jar_for, save_app_config


def _cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path)


def _all_ok(monkeypatch):
    monkeypatch.setattr(preflight, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(preflight, "browser_available", lambda cfg: True)
    monkeypatch.setattr(preflight, "session_present", lambda cfg: True)
    monkeypatch.setattr(preflight, "llm_configured", lambda cfg: True)


# --------------------------------------------------------------- selection

def test_job_kind_decides_which_prerequisites_apply(monkeypatch, tmp_path):
    """A .mai.json run never touches ffmpeg or the browser: refusing it for
    those would block offline segmentation for no reason."""
    monkeypatch.setattr(preflight, "ffmpeg_available", lambda: False)
    monkeypatch.setattr(preflight, "browser_available", lambda cfg: False)
    monkeypatch.setattr(preflight, "session_present", lambda cfg: False)
    monkeypatch.setattr(preflight, "llm_configured", lambda cfg: True)
    cfg = _cfg(tmp_path)

    assert preflight.missing_requirements(cfg, "run") == [
        "ffmpeg", "browser", "session"]
    assert preflight.missing_requirements(cfg, "process") == []


def test_llm_is_only_required_when_segmentation_asks_for_it(monkeypatch, tmp_path):
    _all_ok(monkeypatch)
    monkeypatch.setattr(preflight, "llm_configured", lambda cfg: False)
    cfg = _cfg(tmp_path)
    assert preflight.missing_requirements(cfg, "run", use_llm=True) == ["llm"]
    assert preflight.missing_requirements(cfg, "run", use_llm=False) == []
    assert preflight.missing_requirements(cfg, "process", use_llm=True) == ["llm"]


def test_everything_present_passes(monkeypatch, tmp_path):
    _all_ok(monkeypatch)
    cfg = _cfg(tmp_path)
    assert preflight.missing_requirements(cfg, "run", use_llm=True) == []
    assert preflight.missing_requirements(cfg, "process", use_llm=True) == []


# ------------------------------------------------------------- real checks

def test_llm_configured_needs_both_key_and_model(tmp_path):
    from mai2srt.config import load_app_config
    cfg = _cfg(tmp_path)
    assert preflight.llm_configured(cfg) is False            # nothing stored
    save_app_config(cfg, {"llm": {"base_url": "https://x",
                                  "api_key": "", "model": "m"}})
    assert preflight.llm_configured(cfg) is False            # key missing
    save_app_config(cfg, {"llm": {"base_url": "https://x",
                                  "api_key": "sk-1", "model": ""}})
    assert preflight.llm_configured(cfg) is False            # model missing
    save_app_config(cfg, {"llm": {"base_url": "https://x",
                                  "api_key": "sk-1", "model": "m"}})
    assert preflight.llm_configured(cfg) is True
    assert load_app_config(cfg)["llm"]["model"] == "m"


def test_session_present_needs_an_account_that_signed_in(tmp_path):
    from mai2srt.config import set_active_account
    cfg = _cfg(tmp_path)
    assert preflight.session_present(cfg) is False           # fresh install
    add_account(cfg, "me")
    # registered but NOT active: the job has no account to drive
    assert preflight.session_present(cfg) is False
    set_active_account(cfg, "me")
    # active but never signed in: no cookie jar yet
    assert preflight.session_present(cfg) is False
    jar = cookie_jar_for(cfg, "me")
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_text("[]", encoding="utf-8")
    assert preflight.session_present(cfg) is True


def test_bundled_chromium_detection(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert preflight.bundled_chromium_present() is False
    (tmp_path / "chromium-1194").mkdir()
    assert preflight.bundled_chromium_present() is True


def test_browser_falls_back_to_the_bundled_chromium(monkeypatch, tmp_path):
    """No channel browser is not a failure when playwright's chromium is
    there: Windows ships Edge, but a stripped box may rely on the bundle."""
    monkeypatch.setattr(preflight, "bundled_chromium_present", lambda: True)
    import mai2srt.transcribe.browser as br
    monkeypatch.setattr(br, "resolve_channel", lambda cfg: None)
    assert preflight.browser_available(_cfg(tmp_path)) is True
    monkeypatch.setattr(preflight, "bundled_chromium_present", lambda: False)
    assert preflight.browser_available(_cfg(tmp_path)) is False


# ---------------------------------------------------------------- endpoint

def test_preflight_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from mai2srt.server.app import create_app
    c = TestClient(create_app(_cfg(tmp_path)))

    with_llm = c.get("/api/preflight", params={"kind": "run", "use_llm": "1"})
    assert with_llm.status_code == 200
    body = with_llm.json()
    # a fresh data dir has no account -> session is missing; the LLM is
    # unconfigured too, and both must be named
    assert body["ok"] is False
    assert "session" in body["missing"] and "llm" in body["missing"]

    no_llm = c.get("/api/preflight", params={"kind": "run", "use_llm": "0"})
    assert "llm" not in no_llm.json()["missing"]

    process = c.get("/api/preflight", params={"kind": "process", "use_llm": "1"})
    assert process.json()["missing"] == ["llm"]

    bad = c.get("/api/preflight", params={"kind": "nonsense"})
    assert bad.status_code == 400
