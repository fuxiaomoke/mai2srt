"""Persistent browser session with our own cookie persistence.

M0 findings baked in (see _research/RESEARCH.md):
- the site's auth session cookie is session-scoped by design; it dies with
  every browser close, so WE persist all context cookies to a jar and
  re-inject on every launch;
- when the jar goes stale, /.auth/login/aad?prompt=none does a silent SSO
  round-trip using the (also jar-persisted) Microsoft identity cookies;
  the final fallback is the account picker — one click, no password.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page, Playwright

from ..config import CHAT_URL, Config

log = logging.getLogger("mai2srt.browser")

AUTH_PROBE_JS = ("async () => { const r = await fetch('/.auth/me'); "
                 "return r.status; }")

#: playwright channel -> (exe name for PATH lookup, well-known install paths)
CHANNEL_HINTS = {
    "chrome": ("chrome.exe", [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]),
    "msedge": ("msedge.exe", [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]),
}


def find_channel_exe(channel: str) -> str | None:
    """Locate a real browser executable for a playwright channel."""
    hint = CHANNEL_HINTS.get(channel)
    if not hint:
        return None
    exe, paths = hint
    hit = shutil.which(exe)
    if hit:
        return hit
    for p in paths:
        if Path(p).exists():
            return p
    return None


def resolve_channel(cfg: Config) -> str | None:
    """Which browser to drive; None = playwright's bundled chromium.

    Preference from config.json ("auto" | "chrome" | "msedge" | "chromium").
    A specific preference still falls back gracefully: other real browsers
    first, bundled chromium last.
    """
    from ..config import browser_pref
    pref = browser_pref(cfg)
    if pref == "chromium":
        return None
    order = ["chrome", "msedge"] if pref == "auto" else [pref]
    order += [c for c in ("chrome", "msedge") if c not in order]
    for ch in order:
        if find_channel_exe(ch):
            return ch
    return None


async def launch(p: Playwright, cfg: Config, headless: bool = False) -> BrowserContext:
    """Persistent context (preferred real browser, else bundled) + jar restore.

    headless=False is for interactive sign-in (the user must see and click);
    headless=True is for transcription runs -- no window pops up. Auth then
    relies entirely on the cookie jar + silent SSO; if both fail the run
    aborts with a sign-in hint instead of waiting for invisible clicks.

    Two hard-won robustness rules:
    - profiles are PER-CHANNEL (<account-profile>/<channel>/): a directory
      written by one browser build (e.g. Chrome 141) can CHECK-crash another
      (e.g. bundled chromium 1194) on incompatible cache files -- observed
      live as exit 0x80000003 right after "Failed to open GraphiteDawnCache";
    - any failed launch QUARANTINES the suspect profile and retries once
      with a fresh one, so profile corruption is self-healing and never
      needs manual directory surgery. The cookie jar (browser-agnostic)
      still restores the session afterwards.
    """
    channel = resolve_channel(cfg)
    base = cfg.profile_dir                      # per-account base dir
    candidates = ([channel] if channel else []) + [None]  # None = bundled
    tried: set[str] = set()
    last_error: Exception | None = None

    for cand in candidates:
        name = cand or "chromium"
        if name in tried:
            continue
        tried.add(name)
        profile = base / name
        kwargs = dict(
            user_data_dir=str(profile),
            headless=headless,
            viewport=None,
            args=["--disable-blink-features=AutomationControlled"]
            + ([] if headless else ["--start-maximized"]),
        )
        for attempt in (1, 2):
            try:
                if cand:
                    ctx = await p.chromium.launch_persistent_context(
                        channel=cand, **kwargs)
                else:
                    ctx = await p.chromium.launch_persistent_context(**kwargs)
                log.info("browser channel: %s%s", name,
                         " (headless)" if headless else "")
                await _restore_jar(ctx, cfg)
                return ctx
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt == 1:
                    log.warning("%s launch failed (%s); retrying with a fresh "
                                "profile", name, str(e).splitlines()[0])
                    _quarantine_profile(profile)
                else:
                    log.error("%s launch failed even with a fresh profile: %s",
                              name, str(e).splitlines()[0])
    raise RuntimeError(
        f"no usable browser could be launched (last error: {last_error})")


def _quarantine_profile(profile: Path) -> None:
    """Move a suspect profile aside so the retry starts clean."""
    if not profile.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = profile.with_name(f"{profile.name}.broken-{stamp}")
    try:
        profile.rename(target)
        log.info("quarantined profile %s -> %s", profile.name, target.name)
    except OSError as e:
        log.warning("could not quarantine %s (%s); deleting instead", profile, e)
        shutil.rmtree(profile, ignore_errors=True)


async def _restore_jar(ctx: BrowserContext, cfg: Config) -> None:
    if cfg.cookie_jar.exists():
        try:
            jar = json.loads(cfg.cookie_jar.read_text(encoding="utf-8"))
            await ctx.add_cookies(jar)
            log.debug("cookie jar restored (%d cookies)", len(jar))
        except Exception as e:  # noqa: BLE001 — stale jar must not be fatal
            log.warning("cookie jar restore failed (ignored): %s", e)


async def save_cookie_jar(ctx: BrowserContext, cfg: Config) -> None:
    try:
        cookies = await ctx.cookies()
        cfg.cookie_jar.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
        log.debug("cookie jar saved (%d cookies)", len(cookies))
    except Exception as e:  # noqa: BLE001
        log.warning("cookie jar save failed (ignored): %s", e)


async def auth_status(page: Page) -> int | None:
    try:
        return await page.evaluate(AUTH_PROBE_JS)
    except Exception:  # noqa: BLE001 — page navigating
        return None


async def open_page(ctx: BrowserContext, url: str = CHAT_URL,
                    timeout_ms: int = 1_800_000) -> Page:
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(timeout_ms)
    await page.goto(url, wait_until="domcontentloaded")
    return page


def login_url(prompt: str) -> str:
    """Easy Auth entry point: prompt=none tries silent SSO, select_account
    lands the user DIRECTLY on the Microsoft account picker (no detour
    through the playground chat page first)."""
    from ..config import BASE_URL
    return (f"{BASE_URL}/.auth/login/aad?prompt={prompt}"
            f"&post_login_redirect_uri={quote('/chat')}")


async def _trigger_login(page: Page, prompt: str) -> bool:
    """Navigate the Easy Auth login redirect; True if signed in after."""
    try:
        await page.goto(login_url(prompt), wait_until="domcontentloaded",
                        timeout=120_000)
    except Exception as e:  # noqa: BLE001 — redirect chains can be flaky
        log.debug("login redirect hiccup (continuing): %s", e)
    for _ in range(15):
        if await auth_status(page) == 200:
            return True
        await asyncio.sleep(1)
    return False


async def wait_for_login(page: Page, cfg: Config, timeout_s: int = 600) -> int | None:
    """Interactive sign-in: the login page is already on screen (visible
    browser); poll until the user completes it, then persist the jar.

    Returns None when the user CLOSES the browser window first -- the close
    event is tracked explicitly; polling alone would confuse a dead context
    with a mid-redirect page (both probe as None) and burn the timeout."""
    ctx = page.context
    closed = asyncio.Event()
    ctx.on("close", lambda *_: closed.set())
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if closed.is_set() or page.is_closed():
            log.info("browser window closed before sign-in completed")
            return None
        await asyncio.sleep(2)
        status = await auth_status(page)
        if status == 200:
            log.info("sign-in detected")
            await save_cookie_jar(page.context, cfg)
            return status
    log.error("still not signed in after %ss", timeout_s)
    return await auth_status(page)


async def ensure_auth(page: Page, cfg: Config, timeout_s: int = 600,
                      interactive: bool = True) -> int | None:
    """jar session -> silent SSO -> (interactive only) account picker.

    Non-interactive callers (headless transcription) stop after silent SSO:
    there is no window to click in, so a stale session must fail fast with
    a sign-in hint instead of burning the timeout.
    """
    status = await auth_status(page)
    if status == 200:
        await save_cookie_jar(page.context, cfg)
        return status

    log.info("not signed in; trying silent SSO (prompt=none)")
    if await _trigger_login(page, "none"):
        log.info("silent SSO ok")
        await save_cookie_jar(page.context, cfg)
        return 200

    if not interactive:
        log.error("session expired and silent SSO failed; "
                  "sign in again via the Settings page / CLI login")
        return await auth_status(page)

    log.info("silent SSO unavailable; account picker opened in the browser - "
             "click your account (password only if asked)")
    await _trigger_login(page, "select_account")
    return await wait_for_login(page, cfg, timeout_s)
