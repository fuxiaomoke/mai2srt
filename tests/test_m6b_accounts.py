"""M6b: multi-account session management + browser channel resolution."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt import config as C
from mai2srt.transcribe import browser as B


def _cfg(tmp_path):
    return C.Config(data_dir=tmp_path)


# ------------------------------------------------------------------ accounts

def test_add_account_free_text_names(tmp_path):
    cfg = _cfg(tmp_path)
    # display names are free text: Chinese, spaces, case all preserved
    assert C.add_account(cfg, "测试") == "测试"
    assert C.add_account(cfg, "My Work!") == "My Work!"
    assert "测试" in C.list_accounts(cfg)
    assert "My Work!" in C.list_accounts(cfg)
    # files get derived ascii slugs regardless of the display name
    jar = C.cookie_jar_for(cfg, "测试")
    assert jar.parent == tmp_path / "accounts"
    assert jar.name.startswith("acc-") and jar.name.endswith(".cookies.json")
    assert C.cookie_jar_for(cfg, "My Work!").name.startswith("my-work")


def test_add_account_rejects_duplicates_and_garbage(tmp_path):
    cfg = _cfg(tmp_path)
    C.add_account(cfg, "work")
    with pytest.raises(ValueError):
        C.add_account(cfg, "work")          # duplicate
    with pytest.raises(ValueError):
        C.add_account(cfg, "default")       # reserved
    with pytest.raises(ValueError):
        C.add_account(cfg, "   ")           # empty after strip
    with pytest.raises(ValueError):
        C.add_account(cfg, "x" * 33)        # overlong
    with pytest.raises(ValueError):
        C.add_account(cfg, "a\nb")          # control chars


def test_slugs_never_collide(tmp_path):
    cfg = _cfg(tmp_path)
    C.add_account(cfg, "Work")
    C.add_account(cfg, "work")
    assert C.cookie_jar_for(cfg, "Work") != C.cookie_jar_for(cfg, "work")


def test_default_account_maps_to_legacy_paths(tmp_path):
    cfg = _cfg(tmp_path)
    assert C.cookie_jar_for(cfg, "default") == tmp_path / "cookies.json"
    assert C.profile_dir_for(cfg, "default") == tmp_path / "browser-profile"


def test_fresh_install_has_no_accounts(tmp_path):
    cfg = _cfg(tmp_path)
    assert C.list_accounts(cfg) == []
    assert C.active_account(cfg) is None
    # properties fall back to the legacy scratch paths without an account
    assert cfg.cookie_jar == tmp_path / "cookies.json"
    assert cfg.profile_dir == tmp_path / "browser-profile"


def test_default_account_can_be_forgotten(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "cookies.json").write_text("[]", encoding="utf-8")
    prof = tmp_path / "browser-profile"
    (prof / "x").mkdir(parents=True)
    C.remove_account(cfg, "default")
    assert not (tmp_path / "cookies.json").exists()
    assert not prof.exists()
    assert C.list_accounts(cfg) == []
    assert C.active_account(cfg) is None


def test_legacy_jar_counts_as_default_account(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "cookies.json").write_text("[]", encoding="utf-8")
    assert "default" in C.list_accounts(cfg)
    assert C.active_account(cfg) == "default"
    # the Config property follows the legacy jar
    assert cfg.cookie_jar == tmp_path / "cookies.json"


def test_add_switch_and_property_follows(tmp_path):
    cfg = _cfg(tmp_path)
    name = C.add_account(cfg, "work")
    assert name == "work"
    C.set_active_account(cfg, "work")
    assert C.active_account(cfg) == "work"
    # Config properties now resolve to the account-specific files
    assert cfg.cookie_jar == tmp_path / "accounts" / "work.cookies.json"
    assert cfg.profile_dir == tmp_path / "accounts" / "work.profile"
    assert "work" in C.list_accounts(cfg)


def test_remove_account_deletes_files_and_falls_back(tmp_path):
    cfg = _cfg(tmp_path)
    C.add_account(cfg, "work")
    C.add_account(cfg, "play")
    C.set_active_account(cfg, "work")
    jar = C.cookie_jar_for(cfg, "work")
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_text("[]", encoding="utf-8")
    prof = C.profile_dir_for(cfg, "work")
    (prof / "x").mkdir(parents=True)
    C.remove_account(cfg, "work")
    assert not jar.exists()
    assert not prof.exists()
    assert "work" not in C.list_accounts(cfg)
    assert C.active_account(cfg) == "play"      # fell to the remaining account
    C.remove_account(cfg, "play")
    assert C.active_account(cfg) is None        # none left -> no active


def test_clear_account_session_keeps_entry(tmp_path):
    cfg = _cfg(tmp_path)
    C.add_account(cfg, "work")
    jar = C.cookie_jar_for(cfg, "work")
    jar.parent.mkdir(parents=True, exist_ok=True)
    jar.write_text("[]", encoding="utf-8")
    C.clear_account_session(cfg, "work")
    assert not jar.exists()
    assert "work" in C.list_accounts(cfg)      # entry survives a clear


# ------------------------------------------------------------------ browser

def test_browser_pref_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    assert C.browser_pref(cfg) == "auto"
    C.set_browser_pref(cfg, "msedge")
    assert C.browser_pref(cfg) == "msedge"
    with pytest.raises(ValueError):
        C.set_browser_pref(cfg, "netscape")
    # corrupt/foreign values degrade to auto, never crash
    (cfg.data_dir).mkdir(exist_ok=True)
    cfg.app_config_file.write_text(json.dumps({"browser": {"channel": "ie"}}),
                                   encoding="utf-8")
    assert C.browser_pref(cfg) == "auto"


def test_resolve_channel_honours_preference(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    available = {"chrome": True, "msedge": True}
    monkeypatch.setattr(B, "find_channel_exe",
                        lambda ch: f"/fake/{ch}" if available.get(ch) else None)

    C.set_browser_pref(cfg, "msedge")
    assert B.resolve_channel(cfg) == "msedge"

    C.set_browser_pref(cfg, "chromium")
    assert B.resolve_channel(cfg) is None        # explicit bundled

    C.set_browser_pref(cfg, "auto")
    assert B.resolve_channel(cfg) == "chrome"    # auto prefers chrome

    # preferred missing -> falls back to the other real browser
    C.set_browser_pref(cfg, "chrome")
    available["chrome"] = False
    assert B.resolve_channel(cfg) == "msedge"

    # nothing real installed -> bundled chromium
    available["msedge"] = False
    assert B.resolve_channel(cfg) is None
