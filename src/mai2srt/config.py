"""Paths and tunables. Everything user-specific lives under one data dir."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://playground.microsoft.ai"
CHAT_URL = f"{BASE_URL}/chat"
MODEL_ID = "mai-transcribe-2"

#: playground hard limits (M0-verified from the site's own config; the
#: site's picker gates the FILE, i.e. RAW bytes -- a live website test
#: transcribed a 23 MiB file whose base64 payload is ~30.7 MiB, so the
#: wire-side x4/3 inflation does NOT count against the limit. The
#: 2026-09-26 "payload budget" theory was disproven; see RESEARCH.md)
MAX_UPLOAD_BYTES = 0x1900000          # 25 MiB, on the raw audio file
MAX_AUDIO_SECONDS = 3615.0            # 60 min 15 s
ACCEPTED_MIMES = {"mp3": "audio/mpeg", "wav": "audio/wav"}

#: compression target sits under the limit (mp3 container overhead)
COMPRESS_TARGET_BYTES = 23 * 1024 * 1024
MP3_BITRATE_MIN_BPS = 64_000
MP3_BITRATE_MAX_BPS = 192_000

#: longest audio that fits MAX_UPLOAD_BYTES in ONE upload at the bitrate
#: floor (25 MiB / 8000 B/s = 3277s) minus a container-overhead margin.
#: Longer audio must be CHUNKED: chunking used to trigger only past the
#: 3615s site gate, so ~52-60min files compressed over the limit and
#: failed every attempt. (Real bug, kept fixed under raw semantics.)
MAX_SINGLE_UPLOAD_SECONDS = 3100.0

#: chunking — chunks never exceed the target (cuts land at/below each
#: boundary); at the 64 kbps floor 3000s costs ~22.9 MiB raw, inside the
#: 25 MiB limit with margin.
CHUNK_TARGET_SECONDS = 3000.0
SILENCE_NOISE_DB = -40.0
SILENCE_MIN_S = 0.3

#: upload behaviour
UPLOAD_ATTEMPTS = 3
RETRY_BACKOFF_S = (5, 10, 20)


def default_data_dir() -> Path:
    env = os.environ.get("MAI2SRT_HOME")
    if env:
        return Path(env)
    return Path.home() / ".mai2srt"


@dataclass
class Config:
    data_dir: Path = field(default_factory=default_data_dir)

    @property
    def profile_dir(self) -> Path:
        # active-account aware: each account owns its browser profile so the
        # Microsoft identity of one account never bleeds into another.
        # No active account (fresh install) -> legacy scratch paths.
        return profile_dir_for(self, active_account(self) or DEFAULT_ACCOUNT)

    @property
    def cookie_jar(self) -> Path:
        return cookie_jar_for(self, active_account(self) or DEFAULT_ACCOUNT)

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def app_config_file(self) -> Path:
        return self.data_dir / "config.json"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# LLM settings (segmentation Pass B). OpenAI-compatible endpoints only; the
# pipeline takes a Splitter protocol so a future GUI can swap backends freely.
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-flash"
    temperature: float = 0.0
    timeout_s: float = 120.0


def load_app_config(cfg: Config) -> dict:
    """Read ~/.mai2srt/config.json; missing/corrupt file -> empty dict."""
    try:
        raw = cfg.app_config_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_app_config(cfg: Config, data: dict) -> None:
    cfg.ensure_dirs()
    cfg.app_config_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def llm_config_from(data: dict) -> LLMConfig:
    """Merge the "llm" section of app config over LLMConfig defaults."""
    section = data.get("llm")
    if not isinstance(section, dict):
        return LLMConfig()
    kwargs = {}
    for f in LLMConfig.__dataclass_fields__:
        if f in section:
            kwargs[f] = section[f]
    try:
        return LLMConfig(**kwargs)
    except TypeError:
        return LLMConfig()


# ---------------------------------------------------------------------------
# playground accounts: one cookie jar + browser profile per account, so the
# user can keep several Microsoft sign-ins and switch between them.
#
# An account has a free-text DISPLAY name (Chinese is fine — it's just a
# label) and a derived filesystem SLUG for its files. The pre-multi-account
# files (cookies.json / browser-profile/) stay mapped to the structural
# "default" account, so existing installs migrate for free.
# ---------------------------------------------------------------------------

ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_ACCOUNT = "default"
MAX_ACCOUNT_NAME = 32


def _session_section(data: dict) -> dict:
    s = data.get("session")
    return s if isinstance(s, dict) else {}


def _accounts(data: dict) -> dict:
    a = _session_section(data).get("accounts")
    return a if isinstance(a, dict) else {}


def _slug_for(name: str, taken: set) -> str:
    """Filesystem slug for a display name: ascii-slug if possible, else a
    stable acc-<hash>; suffixed until unique."""
    base = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-_")
    if not base or not ACCOUNT_RE.match(base):
        base = "acc-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    slug, i = base, 2
    while slug in taken:
        slug = f"{base}-{i}"
        i += 1
    return slug


def _account_slug(cfg: Config, name: str) -> str | None:
    if name == DEFAULT_ACCOUNT:
        return DEFAULT_ACCOUNT
    entry = _accounts(load_app_config(cfg)).get(name)
    return entry.get("slug") if isinstance(entry, dict) else None


def cookie_jar_for(cfg: Config, name: str) -> Path:
    if name == DEFAULT_ACCOUNT:
        return cfg.data_dir / "cookies.json"
    slug = _account_slug(cfg, name) or _slug_for(name, set())
    return cfg.data_dir / "accounts" / f"{slug}.cookies.json"


def profile_dir_for(cfg: Config, name: str) -> Path:
    if name == DEFAULT_ACCOUNT:
        return cfg.data_dir / "browser-profile"
    slug = _account_slug(cfg, name) or _slug_for(name, set())
    return cfg.data_dir / "accounts" / f"{slug}.profile"


def list_accounts(cfg: Config) -> list[str]:
    """Registered accounts. EMPTY on a fresh install -- the user signs in
    once and that first login becomes the first card. The pre-multi-account
    jar (cookies.json) surfaces as the legacy "default" account."""
    names = set(_accounts(load_app_config(cfg)).keys())
    if (cfg.data_dir / "cookies.json").exists():
        names.add(DEFAULT_ACCOUNT)
    rest = sorted(n for n in names if n != DEFAULT_ACCOUNT)
    return ([DEFAULT_ACCOUNT] if DEFAULT_ACCOUNT in names else []) + rest


def active_account(cfg: Config) -> str | None:
    """The account transcription uses; None when nothing is registered.
    A legacy pre-multi-account jar is adopted as the active default."""
    name = _session_section(load_app_config(cfg)).get("active")
    if isinstance(name, str) and name and name in list_accounts(cfg):
        return name
    if DEFAULT_ACCOUNT in list_accounts(cfg):
        return DEFAULT_ACCOUNT
    return None


def set_active_account(cfg: Config, name: str) -> None:
    data = load_app_config(cfg)
    s = data.setdefault("session", {})
    s["active"] = name
    if name != DEFAULT_ACCOUNT:
        s.setdefault("accounts", {}).setdefault(
            name, {"slug": _slug_for(name, {DEFAULT_ACCOUNT})})
    save_app_config(cfg, data)


def add_account(cfg: Config, name: str) -> str:
    """Register an account (empty session until the user signs in).

    The name is a free-text display label (Chinese welcome); files get a
    derived slug. Raises ValueError on empty/overlong/control-char names
    or duplicates.
    """
    name = name.strip()
    if (not name or len(name) > MAX_ACCOUNT_NAME
            or any(ord(c) < 32 for c in name)):
        raise ValueError(f"invalid account name: {name!r}")
    data = load_app_config(cfg)
    s = data.setdefault("session", {})
    accounts = s.setdefault("accounts", {})
    if name == DEFAULT_ACCOUNT or name in accounts:
        raise ValueError(f"account already exists: {name!r}")
    taken = {e.get("slug") for e in accounts.values() if isinstance(e, dict)}
    taken.add(DEFAULT_ACCOUNT)
    accounts[name] = {
        "slug": _slug_for(name, taken),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    save_app_config(cfg, data)
    return name


def remove_account(cfg: Config, name: str) -> None:
    """Forget an account entirely: cookies + browser profile + entry.

    Any account can be forgotten -- that is the whole point of the button.
    If it was active, activation moves to the first remaining account
    (or nowhere when none remain)."""
    cookie_jar_for(cfg, name).unlink(missing_ok=True)
    shutil.rmtree(profile_dir_for(cfg, name), ignore_errors=True)
    data = load_app_config(cfg)
    s = data.setdefault("session", {})
    s.get("accounts", {}).pop(name, None)
    if s.get("active") == name:
        remaining = sorted(n for n in s.get("accounts", {}) if n != name)
        if name != DEFAULT_ACCOUNT and (cfg.data_dir / "cookies.json").exists():
            remaining.insert(0, DEFAULT_ACCOUNT)
        if remaining:
            s["active"] = remaining[0]
        else:
            s.pop("active", None)
    save_app_config(cfg, data)


def clear_account_session(cfg: Config, name: str) -> None:
    """Clear a session: drop the cookies + browser profile, keep the entry."""
    cookie_jar_for(cfg, name).unlink(missing_ok=True)
    shutil.rmtree(profile_dir_for(cfg, name), ignore_errors=True)


# ---------------------------------------------------------------------------
# browser preference: which real browser Playwright drives (or the bundled
# chromium). "auto" tries chrome then msedge then bundled.
# ---------------------------------------------------------------------------

BROWSER_CHANNELS = ("auto", "chrome", "msedge", "chromium")


def browser_pref(cfg: Config) -> str:
    b = load_app_config(cfg).get("browser")
    ch = b.get("channel") if isinstance(b, dict) else None
    return ch if ch in BROWSER_CHANNELS else "auto"


def set_browser_pref(cfg: Config, channel: str) -> None:
    if channel not in BROWSER_CHANNELS:
        raise ValueError(f"unknown browser channel: {channel!r}")
    data = load_app_config(cfg)
    data["browser"] = {"channel": channel}
    save_app_config(cfg, data)


# ---------------------------------------------------------------------------
# subtitle parameters: the 8 user-owned knobs (persisted so BOTH transcribe
# jobs and refine previews use them — the user approved "转录页也用持久化参数").
# Keys match the UI slider specs; bounds mirror the UI ranges.
# ---------------------------------------------------------------------------

#: key -> (default, min, max)
SUBTITLE_PARAM_SPECS: dict[str, tuple[float, float, float]] = {
    "max_duration": (12.0, 4.0, 30.0),
    "max_chars": (60.0, 20.0, 120.0),
    "min_duration": (1.2, 0.0, 4.0),
    "min_chars": (5.0, 0.0, 20.0),
    "split_pause": (0.8, 0.2, 2.0),
    "merge_gap": (0.8, 0.2, 3.0),
    "tolerance": (0.2, 0.0, 1.0),
    "expand": (0.25, 0.0, 0.6),
}


def subtitle_param_defaults() -> dict[str, float]:
    return {k: v[0] for k, v in SUBTITLE_PARAM_SPECS.items()}


def subtitle_params(cfg: Config) -> dict[str, float]:
    """Effective subtitle params: stored section merged over defaults."""
    stored = load_app_config(cfg).get("subtitle_params")
    out = subtitle_param_defaults()
    if isinstance(stored, dict):
        for k, (default, lo, hi) in SUBTITLE_PARAM_SPECS.items():
            v = stored.get(k)
            if isinstance(v, (int, float)) and lo <= v <= hi:
                out[k] = float(v)
    return out


def set_subtitle_params(cfg: Config, params: dict) -> dict[str, float]:
    """Validate + persist; unknown/out-of-range keys are ignored. Returns the
    effective set (so the caller can echo authoritative values to the UI)."""
    data = load_app_config(cfg)
    merged = subtitle_params(cfg)
    for k, (default, lo, hi) in SUBTITLE_PARAM_SPECS.items():
        v = params.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            merged[k] = float(v)
    data["subtitle_params"] = merged
    save_app_config(cfg, data)
    return merged


# ---------------------------------------------------------------------------
# audio bindings: a per-mai.json user-picked audio file for refine-page
# playback (差分音轨 replacement, or the transcription-time source moved).
# Stored as a plain map {mai_json_path: audio_path}; selecting audio is NOT
# an edit, so it lives here rather than in the edit record.
# ---------------------------------------------------------------------------

def audio_override(cfg: Config, mai_json_path) -> str | None:
    m = load_app_config(cfg).get("audio_overrides")
    if not isinstance(m, dict):
        return None
    v = m.get(str(mai_json_path))
    return v if isinstance(v, str) and v else None


def set_audio_override(cfg: Config, mai_json_path, audio_path) -> None:
    """Bind an audio file to one mai.json (None/"" clears the binding)."""
    data = load_app_config(cfg)
    m = data.get("audio_overrides")
    if not isinstance(m, dict):
        m = {}
    key = str(mai_json_path)
    if audio_path:
        m[key] = str(audio_path)
    else:
        m.pop(key, None)
    if m:
        data["audio_overrides"] = m
    else:
        data.pop("audio_overrides", None)
    save_app_config(cfg, data)


# ---------------------------------------------------------------------------
# conversation cleanup: whether a finished transcription deletes its
# playground conversation. Default ON -- the client has done this since day
# one (verified live 2026-09-25: zero mai2srt conversations left server-side);
# the toggle lets the user OPT OUT and keep a visible record on the site.
# ---------------------------------------------------------------------------

def auto_delete_conversation(cfg: Config) -> bool:
    v = load_app_config(cfg).get("delete_conversation")
    return v if isinstance(v, bool) else True


def set_auto_delete_conversation(cfg: Config, on: bool) -> None:
    data = load_app_config(cfg)
    data["delete_conversation"] = bool(on)
    save_app_config(cfg, data)


def last_mai_json(cfg: Config) -> str | None:
    p = load_app_config(cfg).get("last_mai_json")
    return p if isinstance(p, str) and p else None


def set_last_mai_json(cfg: Config, path) -> None:
    data = load_app_config(cfg)
    data["last_mai_json"] = str(path)
    save_app_config(cfg, data)


# ---------------------------------------------------------------------------
# projects storage: the dedicated folder holding .mai.json transcripts (plus
# their paired .edit.json records). User content stays SEPARATE from app
# data (~/.mai2srt) so packaging/upgrade/uninstall never touches results,
# and "back up my work" means copying one visible folder.
# ---------------------------------------------------------------------------

def _documents_dir() -> Path:
    """The user's Documents folder via the Windows Known Folder API --
    correct under OneDrive redirection; ~/Documents elsewhere."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            guid = _GUID(0xFDD39AD0, 0x238F, 0x46AF,
                         (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85,
                                              0x48, 0x03, 0x69, 0xC7))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0:
                p = Path(ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                return p
        except Exception:  # noqa: BLE001 -- any failure falls back below
            pass
    return Path.home() / "Documents"


def default_projects_dir() -> Path:
    return _documents_dir() / "mai2srt"


def projects_dir(cfg: Config) -> Path:
    """The effective projects folder (dedicated .mai.json storage)."""
    s = load_app_config(cfg).get("storage")
    v = s.get("projects_dir") if isinstance(s, dict) else None
    return Path(v) if isinstance(v, str) and v else default_projects_dir()


def set_projects_dir(cfg: Config, path) -> None:
    data = load_app_config(cfg)
    s = data.get("storage")
    if not isinstance(s, dict):
        s = {}
        data["storage"] = s
    s["projects_dir"] = str(path)
    save_app_config(cfg, data)
