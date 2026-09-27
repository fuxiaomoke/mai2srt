"""Pre-flight checks: is this job worth starting at all?

A transcription needs three local things before it can possibly work -- a
usable ffmpeg, a browser to drive, and a signed-in playground account -- and
BOTH job kinds need a configured LLM endpoint whenever segmentation is
switched on. Starting anyway means the user watches a job die a minute later
on a message written for a developer; the button must refuse up front and name
what is missing.

The checks are STATIC on purpose (no browser launch, no network round-trip):
the click has to stay instant. The live probe still happens inside the job
(``browser.ensure_auth``), which is the only place an EXPIRED session can be
caught -- cookies on disk cannot prove otherwise.

Keys returned here are stable identifiers, never prose: the UI owns the
wording and its translations.
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import Config, active_account, cookie_jar_for, load_app_config

#: job kinds the app can start (mirrors the two button states on the page)
KIND_RUN = "run"          # audio/video -> transcribe -> segment -> SRT
KIND_PROCESS = "process"  # existing .mai.json -> segment -> SRT

#: identifiers, in the order a toast should list them
REQUIREMENTS = ("ffmpeg", "browser", "session", "llm")


def missing_requirements(cfg: Config, kind: str = KIND_RUN,
                         use_llm: bool = True) -> list[str]:
    """Prerequisite keys this job is missing; empty list = start it."""
    missing: list[str] = []
    if kind != KIND_PROCESS:
        if not ffmpeg_available():
            missing.append("ffmpeg")
        if not browser_available(cfg):
            missing.append("browser")
        if not session_present(cfg):
            missing.append("session")
    if use_llm and not llm_configured(cfg):
        missing.append("llm")
    return missing


def ffmpeg_available() -> bool:
    """The same binary the pipeline would run (bundled first, then PATH)."""
    from .media.probe import resolve_tool
    try:
        resolve_tool("ffmpeg")
        return True
    except Exception:  # noqa: BLE001 -- any resolution failure is "missing"
        return False


def browser_available(cfg: Config) -> bool:
    """A real channel browser, or playwright's bundled chromium.

    Windows always ships Edge, so this is a safety net for stripped systems
    rather than a common failure -- but a job that cannot open a browser must
    not start.
    """
    from .transcribe.browser import find_channel_exe, resolve_channel
    channel = resolve_channel(cfg)
    if channel:
        return find_channel_exe(channel) is not None
    return bundled_chromium_present()


def bundled_chromium_present() -> bool:
    """Does playwright have a chromium to launch?

    Only consulted when no channel browser was found. Playwright keeps its
    browsers in ms-playwright (its own env var wins when set); a missing
    directory there means it would raise "Executable doesn't exist" at launch,
    which is exactly what this check exists to catch.
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        base = Path(env)
    else:
        local = os.environ.get("LOCALAPPDATA")
        base = (Path(local) if local else Path.home() / ".cache") / "ms-playwright"
    try:
        return any(p.name.startswith("chromium") for p in base.iterdir())
    except OSError:
        return False


def session_present(cfg: Config) -> bool:
    """An account is registered AND its cookie jar exists.

    Static by design: an expired session still passes here and is reported by
    the job's own auth probe, which can actually ask the site.
    """
    name = active_account(cfg)
    if not name:
        return False
    return cookie_jar_for(cfg, name).exists()


def llm_configured(cfg: Config) -> bool:
    """Mirrors runner._make_splitter's condition: a resolved endpoint with
    both a key and a model, otherwise segmentation silently degrades."""
    from .segment.llm import resolve_endpoint
    try:
        endpoint = resolve_endpoint(load_app_config(cfg))
    except Exception:  # noqa: BLE001 -- corrupt config counts as unconfigured
        return False
    return bool(endpoint.api_key and endpoint.model)
