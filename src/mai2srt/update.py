"""Update awareness: is there a newer release than the one running?

The app ships as an NSIS installer attached to a GitHub Release tagged
``vX.Y.Z`` (see .github/workflows/release.yml). This module answers exactly one
question -- "is there something newer than me?" -- and hands the answer to the
UI. It deliberately does NOT download or install anything.

Why not the Tauri updater (decided with the user, 2026-11-16): its signature is
not optional, and with it comes a minisign keypair whose loss permanently
strands every installed copy -- the public half is compiled into each build, so
the only recovery is asking everyone to reinstall by hand. That is a permanent
liability taken on for a convenience, on a 0.x project whose releases are
occasional. The first version therefore TELLS the user and links to the release
page. The payload shape below is what a static ``latest.json`` carries, so
moving to automatic installation later is a fetcher, not a redesign.

Cadence lives HERE rather than in the UI. One in-process cache with a 24 h TTL
makes "check at startup, then at most once a day" a single rule: the shell asks
on boot, a long-lived window asks again on a timer, and a reopened window or a
second request cannot multiply the calls to GitHub (60 requests/hour/IP
unauthenticated -- a busy user could otherwise drain it by clicking).

Failure is a non-event: no exception escapes, the caller gets an ``error``
string, and the UI stays silent unless the user asked for the check themselves.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime
from typing import Any

from . import __version__
from .config import Config, load_app_config, save_app_config
from .net import USER_AGENT

#: where an update is looked up. Overridable from config.json ("update_url") so
#: a mirror or a fork can point somewhere else without touching code. Both the
#: GitHub API shape and a static ``latest.json`` are understood.
DEFAULT_UPDATE_URL = "https://api.github.com/repos/fuxiaomoke/mai2srt/releases/latest"

#: how long one answer is trusted. This single number IS the policy.
CACHE_TTL_S = 24 * 3600

#: automatic checks are on by default (the point of the feature) but can be
#: turned off -- some users are behind proxies, or simply do not want the app
#: phoning home on a timer.
AUTO_CHECK_DEFAULT = True

#: release bodies can be long (ours are bilingual, ~4 KB); the answer travels
#: to the webview in one JSON response, so cap it rather than trusting the API.
NOTES_MAX_CHARS = 20000


# ------------------------------------------------------------------- version

def parse_version(text: Any) -> tuple[int, int, int] | None:
    """``v0.1.5`` / ``0.1.5`` / ``0.1.5-beta.2`` -> ``(0, 1, 5)``; else None.

    Only the leading numeric triplet takes part in the comparison. A suffix is
    not a reason to refuse: pre-releases are the channel's decision, and a
    local build reports exactly the version it was cut from. Refusing to
    compare would silently disable update awareness for that install.
    """
    if not isinstance(text, str):
        return None
    parts: list[int] = []
    for chunk in text.strip().lstrip("vV").split(".")[:3]:
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            return None
        parts.append(int(digits))
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else None


def is_newer(candidate: str, current: str) -> bool:
    """Numeric (not lexical) comparison: 0.1.10 IS newer than 0.1.9.

    An unparseable version on either side answers False -- never nag the user
    about something we could not actually compare.
    """
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def release_from_payload(payload: Any, current: str) -> dict | None:
    """A GitHub release object (or a static latest.json) -> our update record.

    Returns None for anything that must not be offered: drafts, pre-releases
    (``/releases/latest`` already excludes both, but a static file or a mirror
    might not), payloads without a usable version, and versions that are not
    newer than the one running.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("draft") or payload.get("prerelease"):
        return None
    version = payload.get("tag_name") or payload.get("version")
    if not isinstance(version, str) or not version.strip():
        return None
    if not is_newer(version, current):
        return None
    # a release page is the friendly target; a static latest.json points at the
    # installer asset instead. Either way it must be a real http(s) URL -- this
    # string ends up in a button that launches the system browser.
    url = payload.get("html_url") or payload.get("url") or ""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        url = ""
    notes = payload.get("body")
    if not isinstance(notes, str) or not notes:
        notes = payload.get("notes") if isinstance(payload.get("notes"), str) else ""
    published = payload.get("published_at") or payload.get("pub_date") or ""
    return {
        "version": version.strip().lstrip("vV"),
        "url": url,
        "notes": notes[:NOTES_MAX_CHARS],
        "published_at": published if isinstance(published, str) else "",
    }


# ---------------------------------------------------------------- settings

def update_settings(cfg: Config) -> dict:
    """``{"auto": bool, "url": str}`` with defaults filled in."""
    data = load_app_config(cfg)
    auto = data.get("update_check")
    url = data.get("update_url")
    return {
        "auto": auto if isinstance(auto, bool) else AUTO_CHECK_DEFAULT,
        "url": url if isinstance(url, str) and url.startswith(("http://", "https://"))
        else DEFAULT_UPDATE_URL,
    }


def set_update_settings(cfg: Config, auto: bool | None = None,
                        url: str | None = None) -> dict:
    data = load_app_config(cfg)
    if auto is not None:
        # Deliberately does NOT clear the cache: a fresh answer stays true
        # whether or not we keep polling, and the TTL already bounds its age --
        # turning checks off must not also hide a release the user was just
        # told about.
        data["update_check"] = bool(auto)
    if url is not None:
        data["update_url"] = str(url)
        clear_cache()      # a different source invalidates every old answer
    save_app_config(cfg, data)
    return update_settings(cfg)


# ------------------------------------------------------------------- check

#: last successful answer, per process. Not per Config: the app has one backend
#: and one version, so a second Config object must not trigger a second fetch.
_cache: dict[str, Any] = {"at": 0.0, "value": None}


def clear_cache() -> None:
    _cache["at"] = 0.0
    _cache["value"] = None


def fetch_json(url: str, timeout_s: float = 6.0) -> Any:
    """GET a JSON document. Raises on anything that is not a clean 2xx parse --
    ``check_for_update`` turns that into a string, this stays loud for tests."""
    req = urllib.request.Request(url, headers={
        # GitHub answers 403 to a request without one; every relay/proxy in
        # between is happier with an identity as well (see net.py)
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8", "replace"))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def check_for_update(cfg: Config, force: bool = False,
                     now: float | None = None) -> dict:
    """The whole answer the UI needs, never raising.

    ``force`` is the manual button: it bypasses BOTH the TTL and the
    auto-check toggle (a click is explicit permission to use the network).

    A FAILED check is not cached: the next attempt should be able to succeed.
    """
    now = time.time() if now is None else now
    settings = update_settings(cfg)
    status: dict[str, Any] = {
        "current": __version__,
        "auto": settings["auto"],
        "url": settings["url"],
        "checked_at": None,
        "latest": None,
        "error": None,
    }

    cached = _cache["value"]
    if not force and isinstance(cached, dict) and now - _cache["at"] < CACHE_TTL_S:
        # the settings can change between checks: report them as they are NOW
        # while keeping the (still fresh) answer they were fetched with
        return dict(cached, auto=settings["auto"], url=settings["url"])

    if not settings["auto"] and not force:
        # automatic checks off: answer with what we know, ask nothing
        return status

    try:
        payload = fetch_json(settings["url"])
        status["latest"] = release_from_payload(payload, __version__)
    except Exception as e:  # noqa: BLE001 -- offline, DNS, 403, bad JSON: all "unknown"
        status["error"] = ("%s: %s" % (type(e).__name__, e))[:300]
        return status

    status["checked_at"] = _iso(now)
    _cache["at"] = now
    _cache["value"] = dict(status)
    return status


# -------------------------------------------------------------------- link

def open_external(url: str) -> None:
    """Hand an http(s) URL to the system browser.

    Done in the backend, like reveal: the webview cannot be trusted to open a
    real browser window (and Tauri's opener plugin would need a capability
    entry for every scheme anyway). ``os.startfile`` on Windows avoids the
    ``cmd /c start`` shim webbrowser falls back to, which flashes a console
    window in a GUI app.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("only http(s) urls can be opened")
    if sys.platform == "win32":
        import os
        os.startfile(url)  # noqa: S606 -- the default browser, no console
    else:
        webbrowser.open(url)
