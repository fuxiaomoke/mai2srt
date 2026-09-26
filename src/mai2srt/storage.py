"""Projects storage: the dedicated folder that keeps .mai.json transcripts
(plus their paired .edit.json records) in ONE user-visible place.

Audio files are NEVER moved -- a project's JSON records where its audio
lives (``source``), and SRT exports follow the AUDIO, not the JSON:

    transcribe / process  ->  <audio stem>.srt        next to the audio
    refine save           ->  <audio stem>_精修.srt    next to the audio
    (no audio on disk     ->  next to the JSON, json stem)

so the initial result and the refined result coexist instead of
overwriting each other, and subtitles always land where the media lives.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import (
    Config, audio_override, default_projects_dir, last_mai_json,
    projects_dir, set_audio_override, set_last_mai_json, set_projects_dir,
)
from .media import playback

#: refine exports append this (zh default; the UI sends a localized one)
DEFAULT_REFINE_SUFFIX = "_精修"

_SFX_HOSTILE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def json_stem(p: Path) -> str:
    """sample.mai.json -> sample (sample.json -> sample)."""
    n = p.name
    if n.lower().endswith(".mai.json"):
        return n[: -len(".mai.json")]
    return p.stem


def is_mai_json(p: Path) -> bool:
    n = p.name.lower()
    return n.endswith(".mai.json") or n.endswith(".json")


def paired_edit_path(p: Path) -> Path:
    """foo.mai.json -> foo.mai.edit.json (foo.json -> foo.edit.json)."""
    n = p.name
    base = n[:-5] if n.lower().endswith(".json") else n
    return p.with_name(base + ".edit.json")


def sanitize_suffix(s: str | None) -> str:
    """A UI-supplied refine suffix -> filesystem-safe, capped; the zh
    default when nothing usable arrives."""
    s = _SFX_HOSTILE.sub("", (s or "").strip())[:24].strip(" .")
    return s or DEFAULT_REFINE_SUFFIX


def ensure_projects_dir(cfg: Config) -> Path:
    d = projects_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mtime_iso(p: Path) -> str | None:
    try:
        return datetime.fromtimestamp(
            p.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


def _read_source(p: Path) -> str | None:
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get("source")
        return v if isinstance(v, str) else None
    except (OSError, ValueError):
        return None


def unique_target(d: Path, filename: str) -> Path:
    """d/filename, then "stem 2.ext", "stem 3.ext"... on collision.
    Understands the double suffix x.mai.json (x 2.mai.json)."""
    p = d / filename
    if not p.exists():
        return p
    n = filename
    if n.lower().endswith(".mai.json"):
        base, ext = n[: -len(".mai.json")], ".mai.json"
    else:
        base, ext = p.stem, p.suffix
    i = 2
    while True:
        p = d / f"{base} {i}{ext}"
        if not p.exists():
            return p
        i += 1


def transcribe_target(cfg: Config, audio: Path) -> Path:
    """Where a fresh transcription's .mai.json lands inside storage.

    Re-transcribing the SAME audio overwrites its project (the wanted
    refresh); a DIFFERENT audio that merely shares the stem gets a
    numbered sibling instead of clobbering it."""
    d = ensure_projects_dir(cfg)
    p = d / (audio.stem + ".mai.json")
    if p.exists() and _read_source(p) not in (None, str(audio)):
        p = unique_target(d, audio.stem + ".mai.json")
    return p


def srt_target(cfg: Config, src: Path, source: str | None,
               refined: bool = False, suffix: str | None = None) -> Path:
    """SRT export target for one mai.json: the AUDIO's folder when the
    audio resolves (override > transcription-time source), else the JSON's
    own folder. `refined` output carries the disambiguating suffix."""
    sfx = sanitize_suffix(suffix) if refined else ""
    for cand in (audio_override(cfg, src), source):
        if cand and Path(cand).is_file():
            ap = Path(cand)
            return ap.with_name(ap.stem + sfx + ".srt")
    return src.with_name(json_stem(src) + sfx + ".srt")


def list_projects(cfg: Config) -> dict:
    """Storage overview for the settings card. Read-only: never creates
    the folder, so a fresh install reports an empty library without
    touching the user's Documents."""
    d = projects_dir(cfg)
    projects = []
    total = 0
    if d.is_dir():
        for p in sorted(d.glob("*.mai.json"), key=lambda x: x.name.lower()):
            try:
                size = p.stat().st_size
            except OSError:
                continue
            total += size
            override = audio_override(cfg, p)
            src = _read_source(p)
            # same resolution as the refine player (video sources resolve
            # to their extraction cache) -- read-only, never extracts
            r = playback.resolve_playable(cfg, override, src)
            projects.append({
                "name": p.name,
                "path": str(p),
                "size": size,
                "mtime": _mtime_iso(p),
                "has_edit": paired_edit_path(p).exists(),
                "audio": {
                    "resolved": r["resolved"],
                    "missing": r["missing"],
                    "pending": r["pending"],
                    "video": playback.is_video(override or src or ""),
                },
            })
    return {"dir": str(d), "default_dir": str(default_projects_dir()),
            "exists": d.is_dir(),
            "projects": projects, "total_bytes": total}


def _rebind(cfg: Config, old: Path, new: Path) -> None:
    """Move the per-project pointers (audio binding, last-opened) from the
    old json path to the new one."""
    a = audio_override(cfg, old)
    if a:
        set_audio_override(cfg, old, None)
        set_audio_override(cfg, new, a)
    if last_mai_json(cfg) == str(old):
        set_last_mai_json(cfg, new)


def import_projects(cfg: Config, paths: list[str], mode: str) -> dict:
    """Bring external .mai.json files into storage (copy, or move which
    also relocates the paired edit record and rebinds pointers)."""
    if mode not in ("copy", "move"):
        raise ValueError("mode must be copy or move")
    d = ensure_projects_dir(cfg)
    inside = d.resolve()
    imported = 0
    skipped: list[str] = []
    for raw in paths:
        src = Path(raw)
        if not src.is_file() or not is_mai_json(src):
            skipped.append(src.name)
            continue
        try:
            if src.resolve().parent == inside:
                skipped.append(src.name)      # already managed here
                continue
        except OSError:
            pass
        dst = unique_target(d, src.name)
        if mode == "move":
            edit = paired_edit_path(src)
            if edit.is_file():
                shutil.move(str(edit), str(paired_edit_path(dst)))
            shutil.move(str(src), str(dst))
            _rebind(cfg, src, dst)
        else:
            shutil.copy2(src, dst)
        imported += 1
    out = list_projects(cfg)
    out["imported"] = imported
    out["skipped"] = skipped
    return out


def delete_project(cfg: Config, path) -> dict:
    """Remove one project + its edit record + its audio binding. Only
    paths that live DIRECTLY in the storage folder are deletable -- this
    must never become a general-purpose file deleter."""
    d = projects_dir(cfg).resolve()
    p = Path(path).resolve()
    if p.parent != d:
        raise ValueError("not a storage project: %s" % path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    p.unlink(missing_ok=True)
    paired_edit_path(p).unlink(missing_ok=True)
    set_audio_override(cfg, p, None)
    out = list_projects(cfg)
    out["deleted"] = p.name
    return out


def move_projects(cfg: Config, new_dir: Path) -> dict:
    """Relocate every managed project into new_dir (edit records follow,
    pointers rebind), then remember new_dir as the storage location."""
    old = projects_dir(cfg)
    new_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    if old.is_dir():
        for p in sorted(old.glob("*.mai.json")):
            dst = unique_target(new_dir, p.name)
            edit = paired_edit_path(p)
            if edit.is_file():
                shutil.move(str(edit), str(paired_edit_path(dst)))
            shutil.move(str(p), str(dst))
            _rebind(cfg, p, dst)
            moved += 1
        # orphaned edit records (their json is already gone) still follow
        for e in old.glob("*.edit.json"):
            if e.is_file():
                shutil.move(str(e), str(unique_target(new_dir, e.name)))
    set_projects_dir(cfg, new_dir)
    out = list_projects(cfg)
    out["moved"] = moved
    return out


def open_project(cfg: Config, path) -> str:
    """Mark a project as the refine target (last_mai_json) so the refine
    page auto-loads it on mount."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    set_last_mai_json(cfg, p)
    return str(p)
