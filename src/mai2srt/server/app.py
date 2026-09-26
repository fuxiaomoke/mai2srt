"""FastAPI application factory for the mai2srt UI backend."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .. import __version__
from ..config import (
    Config, audio_override, auto_delete_conversation, last_mai_json,
    set_audio_override, set_auto_delete_conversation, set_last_mai_json,
    set_subtitle_params, subtitle_params,
)
from ..postprocess import PostprocessParams
from ..segment import SegmentParams
from ..media import playback
from ..media.playback import DIRECT_PLAY_MIMES
from ..storage import paired_edit_path
from ..transcribe.persist import load_mai_json
from .. import storage
from . import llm_admin
from .jobs import BusyError, Job, JobManager


#: pydantic DTOs live at MODULE level: `from __future__ import annotations`
#: makes annotations strings, and FastAPI resolves them via func.__globals__ --
#: classes nested inside create_app would be invisible and degrade to query
#: params (bit us once: PUT /api/llm/providers -> "query p missing").
class ProviderIn(BaseModel):
    id: str | None = None
    name: str
    protocol: str = "openai"
    base_url: str = ""
    api_key: str = ""
    auth_style: str = "bearer"
    models: list[dict] = []


class DiscoverApplyIn(BaseModel):
    models: list[dict]          # full new model list to store
    keep_manual: bool = True    # keep manual/builtin entries not in discovery


class ActiveIn(BaseModel):
    provider: str
    model: str
    effort: str | None = "low"


class RunIn(BaseModel):
    kind: str                       # run | process | login
    audio_path: str | None = None
    mai_json_path: str | None = None
    use_llm: bool = True
    params: dict = {}


class PreviewIn(BaseModel):
    mai_json_path: str
    use_llm: bool = False
    params: dict = {}


class RenderIn(BaseModel):
    mai_json_path: str
    entries: list[dict]           # [{w0, w1, text, dialogue}] — user-edited
    params: dict = {}
    #: refine exports carry a localized suffix (_精修 / _refined) so they
    #: coexist with the initial transcription result instead of clobbering
    name_suffix: str | None = None


class ParamsIn(BaseModel):
    params: dict


class AudioBindIn(BaseModel):
    mai_json_path: str
    audio_path: str | None = None     # None/"" clears the binding


class AudioPrepareIn(BaseModel):
    mai_json_path: str


class CacheDelIn(BaseModel):
    files: list[str]


#: extensions the WebView2/Chromium <audio> element can actually decode.
#: Owned by media/playback.py now (the extraction decision and the
#: binding validation share the same truth); kept as an alias here.
_AUDIO_MIMES = DIRECT_PLAY_MIMES


class AccountIn(BaseModel):
    name: str


class DeleteModeIn(BaseModel):
    delete: bool = True


class BrowserIn(BaseModel):
    channel: str                  # auto | chrome | msedge | chromium


class RevealIn(BaseModel):
    path: str


class StorageDirIn(BaseModel):
    dir: str
    move: bool = True                 # relocate existing projects?


class ImportIn(BaseModel):
    paths: list[str]
    mode: str = "copy"                # copy | move


class PathIn(BaseModel):
    path: str


def entry_records(words_sorted: list, entries: list) -> list[dict]:
    """SubEntry list -> word-index-range records ({w0,w1,text,dialogue}).

    Shared by the refine preview and the transcription-time refine hand-off;
    ``words_sorted`` must be the same word objects the entries reference,
    sorted by start (index stability is what w0/w1 mean).
    """
    widx = {id(w): i for i, w in enumerate(words_sorted)}
    out: list[dict] = []
    for e in entries:
        idxs = [widx[id(w)] for w in e.words if id(w) in widx]
        if not idxs:
            continue
        out.append({
            "w0": min(idxs), "w1": max(idxs),
            "text": e.text, "dialogue": e.is_dialogue,
        })
    return out


#: Any local page may talk to this API. A HARDCODED port here is a trap: the
#: Vite dev server moved from 5173 to 5183 (app/vite.config.ts) and the stale
#: allowlist made the browser block EVERY response while the server kept
#: logging 200 OK -- the UI showed "backend offline" with a healthy backend.
#: The regex also covers a port bump and any other local tool.
_LOCAL_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def quiet_disconnect_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Silence ONE benign asyncio-on-Windows artefact, loudly keep the rest.

    Chromium's <audio> element aggressively cancels in-flight Range requests
    (seek / enough buffered); on Windows the abort arrives as a RST, and the
    proactor transport's _call_connection_lost then calls shutdown() on the
    already-dead socket -> ConnectionResetError traceback per cancelled
    request. Harmless (the next 206 succeeds), but it floods the console
    whenever the refine audio card is scrubbed. The filter matches ONLY
    that combination; anything else goes to the default handler."""
    def handler(l: asyncio.AbstractEventLoop, ctx: dict) -> None:
        exc = ctx.get("exception")
        msg = ctx.get("message") or ""
        if isinstance(exc, ConnectionResetError) and "_call_connection_lost" in msg:
            return
        l.default_exception_handler(ctx)
    loop.set_exception_handler(handler)


def create_app(cfg: Config) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # see quiet_disconnect_handler: silence the Windows proactor noise
        # from cancelled audio-range requests, keep everything else loud
        quiet_disconnect_handler(asyncio.get_running_loop())
        yield

    app = FastAPI(title="mai2srt", version=__version__, lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        # packaged webview origins. Windows WebView2 cannot use custom
        # schemes, so Tauri serves the frontend from http://tauri.localhost
        # there; macOS/Linux use the tauri:// scheme. Missing the Windows
        # form left every packaged fetch CORS-blocked CLIENT-side while
        # uvicorn kept logging 200 OK (dev matched the regex below, so it
        # only broke in installed builds -- curl-based checks cannot see
        # this, only the browser enforces CORS).
        allow_origins=[
            "tauri://localhost",       # packaged: macOS / Linux
            "http://tauri.localhost",  # packaged: Windows WebView2
        ],
        allow_origin_regex=_LOCAL_ORIGIN_RE,
        allow_methods=["*"], allow_headers=["*"],
    )
    manager = JobManager()
    app.state.cfg = cfg
    app.state.jobs = manager

    # ------------------------------------------------------------------ sys

    @app.get("/api/system")
    def system() -> dict:
        from ..config import browser_pref
        from ..transcribe.browser import find_channel_exe, resolve_channel
        effective = resolve_channel(cfg)
        browsers = {}
        for ch in ("chrome", "msedge"):
            p = find_channel_exe(ch)
            browsers[ch] = {"found": bool(p), "path": p}
        browsers["chromium"] = {"found": True, "path": None}  # playwright bundled
        return {
            "version": __version__,
            "python": sys.version.split()[0],
            "data_dir": str(cfg.data_dir),
            "ffmpeg": _find_tool("ffmpeg"),
            "ffprobe": _find_tool("ffprobe"),
            # legacy shape: the EFFECTIVE browser the next launch will drive
            "chrome": {
                "found": True,
                "channel": effective or "chromium",
                "path": find_channel_exe(effective) if effective else None,
            },
            "browsers": browsers,
            "browser": browser_pref(cfg),
            "cookie_jar": cfg.cookie_jar.exists(),
            "busy": manager.busy(),
        }

    @app.put("/api/browser")
    def browser_set(body: BrowserIn) -> dict:
        from ..config import set_browser_pref
        try:
            set_browser_pref(cfg, body.channel)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "channel": body.channel}

    # -------------------------------------------------------------- session

    @app.get("/api/session")
    def session_info() -> dict:
        from ..config import active_account, cookie_jar_for, list_accounts
        accounts = []
        for n in list_accounts(cfg):
            jar = cookie_jar_for(cfg, n)
            accounts.append({
                "name": n,
                "has_cookies": jar.exists(),
                "updated": _mtime_iso(jar),
            })
        return {"active": active_account(cfg), "accounts": accounts,
                "delete_conversation": auto_delete_conversation(cfg)}

    @app.post("/api/session/accounts")
    def session_add(body: AccountIn) -> dict:
        from ..config import add_account, set_active_account
        try:
            name = add_account(cfg, body.name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        # a fresh account becomes active so the next login targets it
        set_active_account(cfg, name)
        return {"ok": True, "name": name}

    @app.put("/api/session/active")
    def session_switch(body: AccountIn) -> dict:
        from ..config import list_accounts, set_active_account
        if body.name not in list_accounts(cfg):
            raise HTTPException(404, "unknown account: %s" % body.name)
        set_active_account(cfg, body.name)
        return {"ok": True, "active": body.name}

    @app.delete("/api/session/accounts/{name}")
    def session_delete(name: str) -> dict:
        from ..config import list_accounts, remove_account
        if name not in list_accounts(cfg):
            raise HTTPException(404, "unknown account: %s" % name)
        remove_account(cfg, name)
        return {"ok": True}

    @app.put("/api/session/delete_mode")
    def session_delete_mode(body: DeleteModeIn) -> dict:
        """Whether finished transcriptions delete their playground
        conversation. ON = leave no trace (the historic default)."""
        set_auto_delete_conversation(cfg, body.delete)
        return {"ok": True, "delete": body.delete}

    # ------------------------------------------------------------------ llm

    @app.get("/api/llm/presets")
    def llm_presets() -> dict:
        return llm_admin.PRESETS

    @app.get("/api/llm/providers")
    def llm_providers() -> dict:
        return {"providers": [llm_admin.provider_public(p)
                              for p in llm_admin.load_providers(cfg)]}

    @app.put("/api/llm/providers")
    def llm_put_provider(p: ProviderIn) -> dict:
        if p.protocol not in llm_admin.PROTOCOLS:
            raise HTTPException(400, "unknown protocol")
        saved = llm_admin.upsert_provider(cfg, p.model_dump())
        return {"provider": llm_admin.provider_public(saved)}

    @app.delete("/api/llm/providers/{pid}")
    def llm_del_provider(pid: str) -> dict:
        if not llm_admin.delete_provider(cfg, pid):
            raise HTTPException(404, "provider not found")
        return {"ok": True}

    @app.post("/api/llm/providers/{pid}/discover")
    def llm_discover(pid: str) -> dict:
        provider = _provider_or_404(pid, unmask=True)
        try:
            discovered = llm_admin.discover_models(provider)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, "discovery failed: %s" % e) from e
        diff = llm_admin.diff_models(provider.get("models", []), discovered)
        return {"diff": diff, "discovered": discovered}

    @app.post("/api/llm/providers/{pid}/discover/apply")
    def llm_discover_apply(pid: str, body: DiscoverApplyIn) -> dict:
        provider = _provider_or_404(pid, unmask=True)
        if body.keep_manual:
            manual = [m for m in provider.get("models", [])
                      if m.get("source") in ("manual", "builtin")]
            by_id = {m["id"]: m for m in manual}
            merged = [by_id.pop(m["id"], None) or m for m in body.models]
            merged.extend(manual_by for manual_by in
                          [m for m in manual if m["id"] in by_id])
            provider["models"] = merged
        else:
            provider["models"] = body.models
        llm_admin.upsert_provider(cfg, provider)
        return {"ok": True, "count": len(provider["models"])}

    @app.post("/api/llm/providers/{pid}/test")
    def llm_test(pid: str, model: str | None = None) -> dict:
        provider = _provider_or_404(pid, unmask=True)
        try:
            return llm_admin.test_provider(provider, model)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, str(e)) from e

    @app.get("/api/llm/active")
    def llm_active() -> dict:
        active = llm_admin.get_active(cfg)
        if not active:
            legacy = _legacy_active(cfg)
            return legacy or {}
        return active

    @app.put("/api/llm/active")
    def llm_set_active(body: ActiveIn) -> dict:
        _provider_or_404(body.provider)
        if body.effort and body.effort not in llm_admin.EFFORT_LEVELS:
            raise HTTPException(400, "bad effort level")
        return {"active": llm_admin.set_active(cfg, body.model_dump())}

    # ------------------------------------------------------ subtitle params

    @app.get("/api/params")
    def params_get() -> dict:
        # the auto-load hand-off only offers a path that still EXISTS. A
        # moved/deleted mai.json otherwise made the refine page auto-load a
        # ghost and fail with a raw 400 on every visit; the stored pointer
        # stays in the config, so restoring the file restores the hand-off.
        last = last_mai_json(cfg)
        if last and not Path(last).exists():
            last = None
        return {"params": subtitle_params(cfg), "last_mai_json": last}

    @app.put("/api/params")
    def params_put(body: ParamsIn) -> dict:
        # user-owned: transcribe jobs AND refine previews both read these
        return {"params": set_subtitle_params(cfg, body.params or {})}

    # ---------------------------------------------------------------- jobs

    @app.post("/api/jobs")
    async def create_job(body: RunIn) -> dict:
        try:
            if body.kind == "run":
                audio = Path(body.audio_path or "")
                if not audio.exists():
                    raise HTTPException(400, "audio not found: %s" % audio)
                job = await manager.start("run", audio.name, lambda j: _run_job(cfg, j, body))
            elif body.kind == "process":
                src = Path(body.mai_json_path or "")
                if not src.exists():
                    raise HTTPException(400, "mai.json not found: %s" % src)
                job = await manager.start("process", src.name, lambda j: _process_job(cfg, j, body))
            elif body.kind == "login":
                from ..runner import login_flow
                job = await manager.start(
                    "login", "sign in",
                    lambda j: login_flow(cfg, on_log=lambda m: manager.emit(j, "log", {"line": m})))
            else:
                raise HTTPException(400, "unknown kind: %s" % body.kind)
        except BusyError as e:
            raise HTTPException(409, str(e)) from e
        return {"job": {"id": job.id, "kind": job.kind, "title": job.title,
                        "status": job.status}}

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": [{"id": j.id, "kind": j.kind, "title": j.title,
                          "status": j.status} for j in manager.list()]}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, from_idx: int = 0):
        job = manager.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")

        async def gen():
            async for ev in manager.stream(job, from_idx):
                yield "event: %s\ndata: %s\n\n" % (
                    ev["type"], json.dumps(ev["data"], ensure_ascii=False))

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        if not manager.cancel(job_id):
            raise HTTPException(409, "job not cancellable")
        return {"ok": True}

    # ---------------------------------------------------------------- files

    @app.post("/api/reveal")
    def reveal(body: RevealIn) -> dict:
        """Open the file's containing folder with the file selected.

        Done server-side: the Tauri opener plugin opened the FILE (and
        silently failed on some associations); users want the folder.
        """
        p = Path(body.path)
        if not p.exists():
            raise HTTPException(404, "not found: %s" % p)
        if sys.platform == "win32":
            # explorer quirk: /select,<path> must be ONE token; exit code is
            # 1 even on success, so fire-and-forget via Popen. A directory
            # opens itself (selecting it in the parent is useless).
            if p.is_dir():
                subprocess.Popen(["explorer", str(p)])
            else:
                subprocess.Popen(["explorer", f"/select,{p}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)] if p.is_file()
                             else ["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return {"ok": True}

    # ---------------------------------------------------------------- audio

    def _audio_info(src: Path, t) -> dict:
        """Effective playback audio for one mai.json.

        Resolution order: user override (差分音轨 / relocated source)
        beats the transcription-time source path. Video / exotic-audio
        sources resolve to their EXTRACTED cache; before the first
        extraction the entry is PENDING (the UI prepares it on load).
        `resolved` is an existing, directly playable file or None;
        `missing` is the would-be path when neither candidate exists."""
        ov = audio_override(cfg, src)
        r = playback.resolve_playable(cfg, ov, t.source or None)
        return {"source": t.source or None, "override": ov,
                "resolved": r["resolved"], "pending": r["pending"],
                "pending_path": r["pending_path"],
                "missing": r["missing"],
                "duration_s": round(t.duration_s, 3)}

    @app.get("/api/audio")
    def audio_stream(path: str, request: Request):
        """Stream a local audio file with Range support.

        Range (206) is not optional polish: Chromium's media element issues
        ranged requests when the user drags the seek bar, and refuses to
        seek at all against some 200-only endpoints.

        Video / exotic-audio paths are transparently served from the
        EXTRACTION CACHE (the first call performs the extraction; sync
        endpoints run in the threadpool so the event loop stays live).
        """
        p = Path(path)
        if playback.needs_extraction(p):
            from ..media.probe import MediaError
            try:
                p = playback.ensure_playable(cfg, p)
            except MediaError as e:
                raise HTTPException(415, str(e)) from e
        if not p.is_file():
            raise HTTPException(404, "audio not found: %s" % p)
        mime = _AUDIO_MIMES.get(p.suffix.lower())
        if mime is None:
            raise HTTPException(415, "unsupported audio type: %s" % p.suffix)
        size = p.stat().st_size
        rng = request.headers.get("range")
        if not rng:
            return FileResponse(p, media_type=mime,
                                headers={"Accept-Ranges": "bytes"})
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if not m or not (m.group(1) or m.group(2)):
            raise HTTPException(416, "bad range: %s" % rng)
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix form: the last N bytes
            start = max(0, size - int(m.group(2)))
            end = size - 1
        if start >= size or end < start:
            raise HTTPException(416, "range not satisfiable: %s" % rng)
        end = min(end, size - 1)
        length = end - start + 1

        def gen():
            with p.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    yield chunk

        return StreamingResponse(
            gen(), status_code=206, media_type=mime,
            headers={"Content-Range": f"bytes {start}-{end}/{size}",
                     "Accept-Ranges": "bytes",
                     "Content-Length": str(length)})

    @app.put("/api/audio_binding")
    def audio_binding(body: AudioBindIn) -> dict:
        """Bind (or clear) the playback audio for one mai.json; echoes the
        resolved audio info so the UI can swap the player in one round trip.
        Videos are welcome too -- they play through the extraction cache
        like any other video source (user decision)."""
        src = Path(body.mai_json_path)
        if not src.exists():
            raise HTTPException(400, "mai.json not found: %s" % src)
        if body.audio_path:
            ap = Path(body.audio_path)
            if not ap.is_file():
                raise HTTPException(400, "audio not found: %s" % ap)
            if (ap.suffix.lower() not in DIRECT_PLAY_MIMES
                    and not playback.is_video(ap)):
                raise HTTPException(400,
                                    "unsupported audio type: %s" % ap.suffix)
            set_audio_override(cfg, src, ap)
        else:
            set_audio_override(cfg, src, None)
        t = load_mai_json(src)
        return {"audio": _audio_info(src, t)}

    @app.post("/api/audio/prepare")
    def audio_prepare(body: AudioPrepareIn) -> dict:
        """Extract the audio track of a PENDING source (video / exotic
        audio). The refine page calls this on load so the first play
        does not hang the media element on a minutes-long transcode;
        the response carries the refreshed audio info."""
        src = Path(body.mai_json_path)
        if not src.exists():
            raise HTTPException(400, "mai.json not found: %s" % src)
        t = load_mai_json(src)
        info = _audio_info(src, t)
        if info.get("pending_path"):
            from ..media.probe import MediaError
            try:
                playback.ensure_playable(cfg, Path(info["pending_path"]))
            except MediaError as e:
                raise HTTPException(415, str(e)) from e
            info = _audio_info(src, load_mai_json(src))
        return {"audio": info}

    # ----------------------------------------------------------- audio cache

    @app.get("/api/audio_cache")
    def audio_cache_get() -> dict:
        return playback.list_cache(cfg)

    @app.post("/api/audio_cache/delete")
    def audio_cache_delete(body: CacheDelIn) -> dict:
        return playback.delete_cache(cfg, body.files)

    @app.post("/api/audio_cache/clear")
    def audio_cache_clear() -> dict:
        return playback.clear_cache(cfg)

    # -------------------------------------------------------------- storage

    @app.get("/api/storage")
    def storage_get() -> dict:
        """Storage-library overview (location + managed projects)."""
        return storage.list_projects(cfg)

    @app.put("/api/storage/dir")
    def storage_dir(body: StorageDirIn) -> dict:
        """Switch the storage location; optionally relocate the projects
        (edit records follow, audio bindings and the last-opened pointer
        rebind to the new paths)."""
        new = Path(body.dir.strip())
        if not str(new) or str(new) in (".", "/", "\\"):
            raise HTTPException(400, "bad storage dir")
        try:
            new.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, "cannot use %s: %s" % (new, e)) from e
        if not new.is_dir():
            raise HTTPException(400, "not a directory: %s" % new)
        try:
            same = new.resolve() == storage.projects_dir(cfg).resolve()
        except OSError:
            same = False
        if same:
            raise HTTPException(400, "already the storage location")
        if body.move:
            return storage.move_projects(cfg, new)
        from ..config import set_projects_dir
        set_projects_dir(cfg, new)
        return storage.list_projects(cfg)

    @app.post("/api/storage/import")
    def storage_import(body: ImportIn) -> dict:
        """Bring external .mai.json files into the library (copy or move)."""
        if not body.paths:
            raise HTTPException(400, "no paths")
        try:
            return storage.import_projects(cfg, body.paths, body.mode)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/storage/project")
    def storage_delete(path: str) -> dict:
        """Delete one managed project (+ edit record + audio binding)."""
        try:
            return storage.delete_project(cfg, path)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/storage/open")
    def storage_open(body: PathIn) -> dict:
        """Make one project the refine target; the refine page auto-loads
        last_mai_json on mount, so the UI navigates right after this."""
        try:
            p = storage.open_project(cfg, body.path)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True, "path": p}

    # ---------------------------------------------------------------- impl

    def _provider_or_404(pid: str, unmask: bool = False) -> dict:
        for p in llm_admin.load_providers(cfg):
            if p.get("id") == pid:
                return p            # stored copy keeps the real key
        raise HTTPException(404, "provider not found: %s" % pid)

    def _legacy_active(cfg: Config) -> dict:
        from ..config import load_app_config, llm_config_from
        llm = llm_config_from(load_app_config(cfg))
        if not llm.api_key:
            return {}
        providers = llm_admin.load_providers(cfg)
        if not providers:
            return {}
        # try to locate the provider whose key matches the legacy endpoint
        for p in providers:
            if p.get("base_url") == llm.base_url and p.get("api_key") == llm.api_key:
                return {"provider": p["id"], "model": llm.model, "effort": "low"}
        return {"provider": providers[0]["id"], "model": llm.model, "effort": "low"}

    def _effective_full(overrides: dict | None) -> dict:
        """Stored user params (full key set), overlaid with per-call numeric
        overrides -- the params that actually shaped a computation."""
        p = subtitle_params(cfg)
        for k, v in (overrides or {}).items():
            if k in p and isinstance(v, (int, float)) and not isinstance(v, bool):
                p[k] = float(v)
        return p

    def _effective_params(overrides: dict | None) -> tuple[SegmentParams, PostprocessParams]:
        p = _effective_full(overrides)
        sp = SegmentParams(
            max_duration_s=p["max_duration"],
            max_chars=int(p["max_chars"]),
            min_duration_s=p["min_duration"],
            min_chars=int(p["min_chars"]),
            split_pause_s=p["split_pause"],
        )
        pp = PostprocessParams(
            max_duration_s=sp.max_duration_s, max_chars=sp.max_chars,
            min_duration_s=sp.min_duration_s,
            merge_gap_threshold_s=p["merge_gap"],
            dialogue_gap_tolerance_s=p["tolerance"],
            mai_expand_s=p["expand"],
        )
        return sp, pp

    async def _run_job(cfg: Config, job: Job, body: RunIn) -> dict:
        from ..runner import run_full
        sp, pp = _effective_params(body.params)
        audio = Path(body.audio_path or "")
        # the .mai.json lands in the storage library (same-audio re-runs
        # overwrite their project; a different same-stem audio siblings);
        # the .srt always lands next to the audio
        res = await run_full(
            cfg, audio,
            json_path=storage.transcribe_target(cfg, audio),
            keep=not auto_delete_conversation(cfg),
            segment_params=sp, post_params=pp,
            use_llm=body.use_llm,
            on_stage=lambda s, d: manager.emit(job, "stage", {"stage": s, "detail": d}),
            on_log=lambda m: manager.emit(job, "log", {"line": m}))
        set_last_mai_json(cfg, res.json_path)
        # refine hand-off: an LLM segmentation is preserved as an origin=llm
        # edit record so "去精修" reopens exactly what transcription produced;
        # a rule-based run clears any stale record (refine then shows the
        # params-driven preview, equivalent to a fresh mai.json load)
        if body.use_llm and res.entry_list and res.word_refs:
            records = entry_records(res.word_refs, res.entry_list)
            _save_edit(res.json_path, records, subtitle_params(cfg), origin="llm")
            manager.emit(job, "log", {
                "line": "LLM 断句已存为精修记录 (%d entries)" % len(records)})
        else:
            stale = _edit_path(res.json_path)
            if stale.exists():
                stale.unlink()

        # video / exotic-audio source: prewarm the playback extraction in
        # the background so the usual "transcribe -> refine -> play" path
        # is instant. Fire-and-forget: playback is a convenience, never a
        # failure of the (already successful) job.
        if playback.needs_extraction(audio):
            async def _prewarm() -> None:
                try:
                    await asyncio.to_thread(
                        playback.ensure_playable, cfg, audio)
                    manager.emit(job, "log", {
                        "line": "音频轨已提取缓存，精修页可直接播放"})
                except Exception as e:  # noqa: BLE001
                    manager.emit(job, "log", {
                        "line": "音频提取预热失败（不影响转录）：%s" % e})
            asyncio.create_task(_prewarm())
        return {"srt_path": str(res.srt_path), "json_path": str(res.json_path),
                "words": res.words, "entries": res.entries, "dialogue": res.dialogue}

    async def _process_job(cfg: Config, job: Job, body: RunIn) -> dict:
        # load + segment + LLM calls are all blocking: run the whole body off
        # the event loop so SSE progress/cancel stay responsive (emissions
        # from the worker thread are marshalled by JobManager.emit)
        return await asyncio.to_thread(_process_job_sync, cfg, job, body)

    def _process_job_sync(cfg: Config, job: Job, body: RunIn) -> dict:
        from ..runner import build_entries
        from ..srt import to_srt
        src = Path(body.mai_json_path or "")
        manager.emit(job, "stage", {"stage": "prepare", "detail": src.name})
        t = load_mai_json(src)
        manager.emit(job, "log", {"line": "loaded %s: %d words" % (src.name, len(t.words))})
        # initial result: <audio stem>.srt next to the AUDIO when it
        # resolves, else next to the json (json stem, .mai stripped)
        out = storage.srt_target(cfg, src, t.source)
        sp, pp = _effective_params(body.params)
        entries, _llm_note = build_entries(
            cfg, t, t.duration_s, segment_params=sp, post_params=pp,
            use_llm=body.use_llm,
            on_stage=lambda s, det: manager.emit(job, "stage", {"stage": s, "detail": det}),
            on_log=lambda m: manager.emit(job, "log", {"line": m}))
        d = sum(1 for e in entries if e.is_dialogue)
        try:
            out.write_text(to_srt(entries), encoding="utf-8")
        except OSError:
            # unwritable audio folder: fall back next to the json
            out = src.with_name(storage.json_stem(src) + ".srt")
            out.write_text(to_srt(entries), encoding="utf-8")
        set_last_mai_json(cfg, src)
        if body.use_llm:
            words = sorted(t.words, key=lambda w: w.start)
            records = entry_records(words, entries)
            _save_edit(src, records, subtitle_params(cfg), origin="llm")
            manager.emit(job, "log", {
                "line": "LLM 断句已存为精修记录 (%d entries)" % len(records)})
        else:
            stale = _edit_path(src)
            if stale.exists():
                stale.unlink()
        return {"srt_path": str(out), "entries": len(entries), "dialogue": d}

    @app.post("/api/preview")
    def preview(body: PreviewIn) -> dict:
        """Structured offline preview for the refine workspace.

        Returns the word list once plus entries as word-index ranges, so the
        UI can edit (retext / merge / split) and recompute timestamps from
        word refs. Blocking (LLM when enabled) but sync endpoints run in
        FastAPI's threadpool, so the event loop stays responsive.
        """
        from ..runner import build_entries
        src = Path(body.mai_json_path)
        if not src.exists():
            raise HTTPException(400, "mai.json not found: %s" % src)
        t = load_mai_json(src)
        if not t.words:
            raise HTTPException(400, "no words in %s" % src.name)
        sp, pp = _effective_params(body.params)
        entries, llm_note = build_entries(cfg, t, t.duration_s, segment_params=sp,
                                          post_params=pp, use_llm=body.use_llm)
        words = sorted(t.words, key=lambda w: w.start)
        out_entries = entry_records(words, entries)
        resp: dict = {
            "words": [{"text": w.text, "start": round(w.start, 3),
                       "end": round(w.end, 3), "speaker": w.speaker}
                      for w in words],
            "entries": out_entries,
            "entries_count": len(out_entries),
            "dialogue": sum(1 for e in out_entries if e["dialogue"]),
            # None = ok/not requested; "unavailable" = no endpoint; else the
            # failure message (results are the deterministic fallback)
            "llm_note": llm_note,
            # playback binding for the refine audio card (override > source)
            "audio": _audio_info(src, t),
        }
        # a successful refine-run LLM split auto-archives exactly like a
        # transcription-time one (origin=llm): refreshing or reopening the
        # file restores it instead of losing the paid result
        if body.use_llm and llm_note is None:
            _save_edit(src, out_entries, _effective_full(body.params),
                       origin="llm")
        edit = _load_edit(src)
        if edit is not None:
            resp["edit"] = edit
        return resp

    @app.post("/api/render")
    def render(body: RenderIn) -> dict:
        """Serialize user-edited entries to .srt + persist the edit record.

        Entry STRUCTURE is authoritative from the UI (retext/merge/split
        already applied); the backend recomputes timestamps from word refs
        and runs only the timeline pass (outward expansion + final format).
        """
        from ..postprocess.dialogue import SubEntry
        from ..postprocess.timeline import expand_outward, final_format
        from ..segment.rules import join_words
        from ..srt import to_srt
        src = Path(body.mai_json_path)
        if not src.exists():
            raise HTTPException(400, "mai.json not found: %s" % src)
        t = load_mai_json(src)
        words = sorted(t.words, key=lambda w: w.start)
        if not words:
            raise HTTPException(400, "no words in %s" % src.name)
        if not body.entries:
            raise HTTPException(400, "no entries")
        _, pp = _effective_params(body.params)
        subs: list[SubEntry] = []
        for i, e in enumerate(body.entries):
            try:
                w0, w1 = int(e.get("w0")), int(e.get("w1"))
            except (TypeError, ValueError):
                raise HTTPException(400, "entry %d: bad w0/w1" % i) from None
            if not (0 <= w0 <= w1 < len(words)):
                raise HTTPException(
                    400, "entry %d: word range %s-%s out of bounds" % (i, w0, w1))
            ws = words[w0:w1 + 1]
            text = e.get("text")
            if not isinstance(text, str) or not text.strip():
                text = join_words(ws)
            subs.append(SubEntry(
                start=ws[0].start, end=ws[-1].end, text=text,
                speakers={w.speaker for w in ws},
                is_dialogue=bool(e.get("dialogue")), words=list(ws)))
        subs.sort(key=lambda s: s.start)
        expand_outward(subs, pp, t.duration_s)
        subs = final_format(subs, pp)
        # refined result: <audio stem><suffix>.srt next to the AUDIO, so it
        # coexists with the initial <audio stem>.srt instead of clobbering
        out = storage.srt_target(cfg, src, t.source, refined=True,
                                 suffix=body.name_suffix)
        try:
            out.write_text(to_srt(subs), encoding="utf-8")
        except OSError:
            # unwritable audio folder: fall back next to the json
            out = src.with_name(storage.json_stem(src)
                                + storage.sanitize_suffix(body.name_suffix)
                                + ".srt")
            out.write_text(to_srt(subs), encoding="utf-8")
        # record the params that SHAPED these entries (request overrides),
        # not whatever the stored config happens to hold right now
        _save_edit(src, body.entries, _effective_full(body.params))
        return {"srt_path": str(out), "entries": len(subs),
                "dialogue": sum(1 for s in subs if s.is_dialogue)}

    @app.post("/api/edit_record")
    def edit_record(body: RenderIn) -> dict:
        """Archive the current session state as a manual edit record WITHOUT
        writing the .srt.

        The refine page fires this before switching files when unsaved work
        is on screen: a file switch must never silently destroy refinement,
        but it also should not force an srt write the user never asked for.
        """
        src = Path(body.mai_json_path)
        if not src.exists():
            raise HTTPException(400, "mai.json not found: %s" % src)
        if not body.entries:
            raise HTTPException(400, "no entries")
        for i, e in enumerate(body.entries):
            try:
                w0, w1 = int(e.get("w0")), int(e.get("w1"))
            except (TypeError, ValueError):
                raise HTTPException(
                    400, "entry %d: bad w0/w1" % i) from None
            if not (0 <= w0 <= w1):
                raise HTTPException(400, "entry %d: bad range" % i)
        _save_edit(src, body.entries, _effective_full(body.params),
                   origin="manual")
        return {"ok": True, "source": src.name}

    # ------------------------------------------------------- edit records

    def _edit_path(src: Path) -> Path:
        """foo.mai.json -> foo.mai.edit.json (shared with storage.py so
        import/move relocate exactly the files this app writes)."""
        return paired_edit_path(src)

    def _load_edit(src: Path) -> dict | None:
        try:
            data = json.loads(_edit_path(src).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("entries") else None
        except (OSError, ValueError):
            return None

    def _save_edit(src: Path, entries: list[dict], params: dict,
                   origin: str = "manual") -> None:
        """origin=llm: the record holds a transcription-time LLM segmentation
        (refine restores it WITHOUT the manual-dirty conflict gate);
        origin=manual: user structure from /api/render (restored as edited)."""
        from datetime import datetime
        payload = {
            "version": 1,
            "source": src.name,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "origin": origin,
            "params": params,
            "entries": entries,
        }
        _edit_path(src).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    def _find_tool(name: str) -> dict:
        # report the SAME binary the pipeline would run: frozen builds
        # prefer the bundled ffmpeg next to the sidecar exe (a status card
        # that shows PATH while the pipeline uses the bundled one would
        # mislead troubleshooting). `source` names which of the two won --
        # the part a bare path cannot convey, and the difference between a
        # dev run and a packaged one.
        try:
            from ..media.probe import resolve_tool_ex
            path, source = resolve_tool_ex(name)
            return {"found": True, "path": path, "source": source}
        except Exception:
            return {"found": False, "path": None, "source": None}

    def _mtime_iso(p: Path) -> str | None:
        try:
            from datetime import datetime
            return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            return None

    return app
