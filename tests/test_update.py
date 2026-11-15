"""Update awareness (2026-11-16): tell the user, never install for them.

The app is released as an NSIS installer on GitHub Releases tagged ``vX.Y.Z``.
``mai2srt.update`` answers one question -- is there something newer than the
copy that is running? -- and the UI shows the answer behind the sidebar
version. No download, no installer, no signing key: see the module docstring
for why the Tauri updater was rejected.

Pinned here:
  * version comparison is NUMERIC (0.1.10 > 0.1.9) and tolerates a ``v`` prefix
    and a pre-release suffix;
  * drafts and pre-releases are never offered;
  * the 24 h cache is the whole cadence policy (startup + at most once a day),
    a manual check forces through it, and a FAILURE is never cached;
  * turning automatic checks off stops the network call while still letting an
    explicit click through;
  * only http(s) can be handed to the system browser.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from mai2srt import update  # noqa: E402
from mai2srt.config import Config  # noqa: E402
from mai2srt.server.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    update.clear_cache()
    yield
    update.clear_cache()


#: Whatever this build is. Every expectation below is expressed against these
#: two constants instead of a literal "0.1.4": the release workflow runs this
#: suite, so a version bump must not turn a green release into a red build
#: (it did once -- scripts/bump.ps1 0.1.5 broke four tests that had hardcoded
#: the previous version as "newer").
CURRENT = update.__version__

#: always newer than anything this project will ever ship
NEW = "99.0.0"


def _cfg(tmp_path) -> Config:
    return Config(data_dir=tmp_path)


def _release(version: str = "v" + NEW, **extra) -> dict:
    payload = {
        "tag_name": version,
        "html_url": "https://github.com/fuxiaomoke/mai2srt/releases/tag/" + version,
        "body": "# mai2srt " + version,
        "published_at": "2026-11-20T10:00:00Z",
    }
    payload.update(extra)
    return payload


# ------------------------------------------------------------------ versions

@pytest.mark.parametrize("text,expected", [
    ("0.1.5", (0, 1, 5)),
    ("v0.1.5", (0, 1, 5)),
    ("V1.2.3", (1, 2, 3)),
    ("0.1.5-beta.2", (0, 1, 5)),
    ("  0.1.5  ", (0, 1, 5)),
    ("0.1", None),
    ("", None),
    ("v", None),
    (None, None),
    (1.5, None),
])
def test_parse_version(text, expected):
    assert update.parse_version(text) == expected


def test_version_comparison_is_numeric_not_lexical():
    assert update.is_newer("0.1.10", "0.1.9")       # lexical would say False
    assert update.is_newer("v0.2.0", "0.1.99")
    assert not update.is_newer("0.1.5", "0.1.5")    # same version is not newer
    assert not update.is_newer("0.1.4", "0.1.5")
    # unparseable sides claim nothing rather than nagging wrongly
    assert not update.is_newer("nightly", "0.1.4")
    assert not update.is_newer("0.1.5", "weird")


# ------------------------------------------------------------------ payloads

def test_newer_release_becomes_an_update_record():
    rec = update.release_from_payload(_release(), CURRENT)
    assert rec is not None
    assert rec["version"] == NEW                    # the "v" is stripped once
    assert rec["url"].endswith("/tag/v" + NEW)
    assert rec["published_at"].startswith("2026-11-20")
    assert "mai2srt v" + NEW in rec["notes"]


@pytest.mark.parametrize("payload", [
    _release("v" + CURRENT),                         # same as running
    _release("v0.0.1"),                              # older
    _release("v99.0.1", draft=True),
    _release("v99.0.1", prerelease=True),
    {"html_url": "https://example.com"},             # no version at all
    "not a dict",
    None,
])
def test_payloads_that_must_not_be_offered(payload):
    assert update.release_from_payload(payload, CURRENT) is None


def test_static_latest_json_shape_is_understood_too():
    """The configurable URL may point at a flat latest.json (the shape an
    automatic-install setup would serve), not only at the GitHub API."""
    rec = update.release_from_payload({
        "version": "99.1.0",
        "notes": "what changed",
        "pub_date": "2026-12-01T00:00:00Z",
        "url": "https://example.com/download/setup.exe",
    }, CURRENT)
    assert rec is not None
    assert rec["version"] == "99.1.0"
    assert rec["url"].endswith("setup.exe")
    assert rec["notes"] == "what changed"


def test_a_non_http_target_is_dropped_not_followed():
    rec = update.release_from_payload(
        _release("v99.9.9", html_url="javascript:alert(1)"), CURRENT)
    assert rec is not None and rec["url"] == ""


# ------------------------------------------------------------------- cadence

def test_one_fetch_serves_the_rest_of_the_day(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(url, timeout_s=6.0):
        calls.append(url)
        return _release()

    monkeypatch.setattr(update, "fetch_json", fake_fetch)
    cfg = _cfg(tmp_path)
    now = 1_000_000.0
    first = update.check_for_update(cfg, now=now)
    assert first["latest"]["version"] == NEW
    assert first["checked_at"]
    # 23 h later: still the cached answer, no second call
    again = update.check_for_update(cfg, now=now + 23 * 3600)
    assert again["latest"]["version"] == NEW
    assert len(calls) == 1
    # past the TTL: asks again
    update.check_for_update(cfg, now=now + 25 * 3600)
    assert len(calls) == 2


def test_manual_check_forces_through_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "fetch_json", lambda url, timeout_s=6.0: _release())
    cfg = _cfg(tmp_path)
    update.check_for_update(cfg, now=1_000_000.0)
    calls = []
    monkeypatch.setattr(
        update, "fetch_json",
        lambda url, timeout_s=6.0: calls.append(url) or _release("v99.0.1"))
    forced = update.check_for_update(cfg, force=True, now=1_000_001.0)
    assert calls and forced["latest"]["version"] == "99.0.1"


def test_automatic_checks_off_means_no_network_but_a_click_still_works(
        tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    update.set_update_settings(cfg, auto=False)
    calls = []

    def fake_fetch(url, timeout_s=6.0):
        calls.append(url)
        return _release()

    monkeypatch.setattr(update, "fetch_json", fake_fetch)
    auto = update.check_for_update(cfg, now=1_000_000.0)
    assert auto["auto"] is False and auto["latest"] is None and not calls
    # the user clicked: explicit permission to use the network
    manual = update.check_for_update(cfg, force=True, now=1_000_000.0)
    assert manual["latest"]["version"] == NEW and calls
    # ... and the answer survives a later automatic poll (which asks nothing)
    later = update.check_for_update(cfg, now=1_000_100.0)
    assert later["latest"]["version"] == NEW and len(calls) == 1


def test_a_failure_is_reported_and_never_cached(tmp_path, monkeypatch):
    def boom(url, timeout_s=6.0):
        raise OSError("no route to host")

    monkeypatch.setattr(update, "fetch_json", boom)
    cfg = _cfg(tmp_path)
    first = update.check_for_update(cfg, now=1_000_000.0)
    assert first["latest"] is None
    assert "OSError" in first["error"] and "no route" in first["error"]
    assert first["checked_at"] is None
    # not cached: the very next call tries again (and can now succeed)
    monkeypatch.setattr(update, "fetch_json", lambda url, timeout_s=6.0: _release())
    second = update.check_for_update(cfg, now=1_000_001.0)
    assert second["error"] is None and second["latest"]["version"] == NEW


def test_settings_roundtrip_and_url_validation(tmp_path):
    cfg = _cfg(tmp_path)
    defaults = update.update_settings(cfg)
    assert defaults["auto"] is True
    assert defaults["url"] == update.DEFAULT_UPDATE_URL
    # a nonsense URL falls back instead of being used
    update.set_update_settings(cfg, url="file:///etc/passwd")
    assert update.update_settings(cfg)["url"] == update.DEFAULT_UPDATE_URL
    # a mirror is honoured
    update.set_update_settings(cfg, url="https://mirror.example/mai2srt/latest.json")
    got = update.update_settings(cfg)
    assert got["url"].startswith("https://mirror.example")
    assert got["auto"] is True


# ------------------------------------------------------------------- over HTTP

def test_update_endpoint_and_toggle(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "fetch_json", lambda url, timeout_s=6.0: _release())
    c = TestClient(create_app(_cfg(tmp_path)))

    st = c.get("/api/update").json()
    assert st["current"] == CURRENT
    assert st["auto"] is True
    assert st["latest"]["version"] == NEW
    assert st["error"] is None

    off = c.put("/api/update", json={"auto": False}).json()
    assert off["auto"] is False

    def explode(url, timeout_s=6.0):
        raise AssertionError("automatic check must not hit the network when off")

    monkeypatch.setattr(update, "fetch_json", explode)
    quiet = c.get("/api/update").json()
    assert quiet["auto"] is False
    # cached manual/previous answer is still allowed through; what matters is
    # that no fetch happened (the explode stub would have raised)
    assert quiet["latest"]["version"] == NEW

    # and the explicit click bypasses the toggle
    monkeypatch.setattr(update, "fetch_json", lambda url, timeout_s=6.0: _release("v99.0.1"))
    forced = c.get("/api/update?force=1").json()
    assert forced["latest"]["version"] == "99.0.1"


def test_open_url_rejects_anything_but_http(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    for bad in ("javascript:alert(1)", "file:///C:/Windows/System32/cmd.exe",
                "cmd.exe", ""):
        r = c.post("/api/open_url", json={"url": bad})
        assert r.status_code == 400, bad
