"""On-demand audio-extraction cache: video (and exotic-audio) sources
for refine-page playback.

The <audio> element only plays what the /api/audio allowlist serves, so
a source outside it (a video file, or wma/aiff/...) gets its audio track
extracted ONCE into an app-data cache; every later open is instant:
  - aac/mp3/opus/vorbis/flac tracks -> -vn -c:a copy remux (lossless,
    IO-bound: seconds for an hour of audio)
  - anything else (ac3/dts/pcm...)  -> aac 192k transcode (~50-100x
    realtime)

SEEKABILITY (2026-11-15). Re-encoding is not the only reason to build a
cache entry: an MP3 is directly playable but NOT correctly seekable in
Chromium/WebView2. The file carries no per-sample index, so dragging the
seek bar makes the element pad the target time into a byte offset from the
Xing header's 100-entry TOC -- and on a VBR file that estimate is seconds
off. Measured on a 55-min VBR source by recording the element's own output
and correlating it against the file: seeking to 10:00 audible at 09:55.7
(-4.3 s), 40:00 at 39:47.9 (-12.1 s), while currentTime -- what the UI
shows -- read the requested time. Subtitle Edit is unaffected because it
scans frames instead of estimating.

The fix is a container swap, not a re-encode: an MP4 sample table plus
-movflags +faststart turns the same MP3 frames into an exactly seekable
stream (measured 0.0 s error, 1.1 s to remux an hour, bit-identical audio).
So `.mp3` / raw `.aac` sources land here too -- via a cache entry named
.m4a -- even though the element could technically play them as they are.

Cache names hash (resolved path + mtime + size), so a changed source
naturally invalidates its entry; index.json maps hashes back to source
paths for the settings-card cache manager. The cache is pure derived
data -- deleting any entry only costs the next first-play extraction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from ..config import Config
from .probe import MediaError, probe, resolve_tool, run_tool

log = logging.getLogger("mai2srt.media")

#: extensions the audio endpoint serves DIRECTLY (mime map + Chromium
#: decoder coverage). Owned here so the whole playback chain -- endpoint
#: allowlist, extraction decision, binding validation -- shares one truth.
DIRECT_PLAY_MIMES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".flac": "audio/flac", ".opus": "audio/ogg",
}

#: video containers the transcribe page accepts (drop + file dialog)
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv",
              ".ts", ".flv"}

#: audio codecs the element decodes natively -> remux instead of
#: transcode (target extension must be in DIRECT_PLAY_MIMES and a
#: container ffmpeg can write via stream copy). mp3 goes into the MP4
#: sample table rather than a raw .mp3 for the seekability reason in the
#: module docstring; the ffmpeg muxer has to be forced (-f mp4), because
#: .m4a alone would pick the ipod muxer, which refuses mp3.
_COPY_TARGET = {"aac": ".m4a", "mp3": ".m4a", "flac": ".flac",
                "opus": ".opus", "vorbis": ".ogg"}

#: targets that need the MP4 muxer + a front-loaded index (moov first, so
#: the element can start ranged seeks without fetching the tail)
_MUX_MP4 = ["-f", "mp4", "-movflags", "+faststart"]

#: directly playable, but Chromium cannot SEEK in them: no sample index
#: (mp3: Xing TOC estimate, seconds off on VBR; raw ADTS aac: worse).
#: These are routed through the cache like an unplayable source.
_NON_SEEKABLE = {".mp3", ".aac"}


def is_video(p) -> bool:
    return Path(p).suffix.lower() in VIDEO_EXTS


def needs_extraction(p) -> bool:
    """True when /api/audio should serve something other than this file.

    Two reasons: the extension is not directly playable (video, wma/...),
    or the element can play it but cannot seek in it accurately (_NON_SEEKABLE,
    replaced by an MP4 container of the same audio).
    """
    suffix = Path(p).suffix.lower()
    return suffix not in DIRECT_PLAY_MIMES or suffix in _NON_SEEKABLE


def audio_cache_dir(cfg: Config) -> Path:
    return cfg.data_dir / "audio-cache"


def cache_key(src: Path) -> str:
    st = src.stat()
    raw = "%s|%d|%d" % (src.resolve(), int(st.st_mtime), st.st_size)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def existing_cache(cfg: Config, src: Path) -> Path | None:
    """The cached playable audio for src, None when not extracted yet.
    Never probes and never extracts: cheap enough for status queries."""
    if not needs_extraction(src):
        return None
    try:
        hits = sorted(audio_cache_dir(cfg).glob(cache_key(src) + ".*"))
    except OSError:
        return None
    for h in hits:
        if ".part" not in h.name:
            return h
    return None


# ----------------------------------------------------------------- extract

def _ffmpeg(args: list[str], dst: Path) -> None:
    cmd = [resolve_tool("ffmpeg"), "-y", "-v", "error", *args, str(dst)]
    r = run_tool(cmd, capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=7200)
    if r.returncode != 0 or not dst.exists():
        raise MediaError(
            f"ffmpeg audio extraction failed: {(r.stderr or '')[-300:]}")


def _index_load(cfg: Config) -> dict:
    try:
        data = json.loads(
            (audio_cache_dir(cfg) / "index.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _index_save(cfg: Config, data: dict) -> None:
    d = audio_cache_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _index_note(cfg: Config, key: str, src: Path) -> None:
    """Record hash -> source so the cache manager can show real names."""
    data = _index_load(cfg)
    cur = data.get(key)
    if isinstance(cur, dict) and cur.get("source") == str(src):
        return
    data[key] = {"source": str(src),
                 "created": datetime.now().isoformat(timespec="seconds")}
    _index_save(cfg, data)


def _index_gc(cfg: Config) -> None:
    d = audio_cache_dir(cfg)
    idx = _index_load(cfg)
    live = {p.name.split(".")[0] for p in d.glob("*")
            if p.name != "index.json" and ".part" not in p.name}
    stale = [k for k in idx if k not in live]
    if stale:
        for k in stale:
            idx.pop(k, None)
        _index_save(cfg, idx)


def _mux_args(ext: str) -> list[str]:
    """Muxer flags for a copy target: .m4a needs -f mp4 forced (the ipod
    muxer rejects mp3) and a front-loaded moov so ranged seeking is cheap."""
    return list(_MUX_MP4) if ext == ".m4a" else []


def ensure_playable(cfg: Config, src) -> Path:
    """A path /api/audio can serve: audio the element plays AND seeks in
    accurately passes through; everything else gets its track extracted into
    the cache (remux when the codec allows, aac transcode otherwise).
    Concurrent-safe via part-file + atomic rename.

    A directly playable source that cannot be probed is served AS-IS rather
    than refused: a peculiar mp3 (or a stray non-audio file with that
    extension) is still better played with the old approximate seek than
    answered with a 415.
    """
    src = Path(src)
    if not src.is_file():
        raise MediaError(f"not found: {src}")
    if not needs_extraction(src):
        return src
    direct = src.suffix.lower() in DIRECT_PLAY_MIMES
    try:
        info = probe(src)      # raises MediaError on a trackless video
    except MediaError:
        if not direct:
            raise
        log.info("seek remux skipped, serving %s as-is (unprobeable)",
                 src.name)
        return src
    d = audio_cache_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    key = cache_key(src)

    # 1) lossless remux when the codec is natively playable
    ext = _COPY_TARGET.get(info.codec)
    if ext:
        dst = d / (key + ext)
        if dst.exists():
            _index_note(cfg, key, src)
            return dst
        tmp = d / (key + ".part" + ext)
        try:
            _ffmpeg(["-i", str(src), "-vn", "-c:a", "copy", *_mux_args(ext)],
                    tmp)
            os.replace(tmp, dst)
            log.info("audio cache (remux %s): %s -> %s",
                     info.codec, src.name, dst.name)
            _index_note(cfg, key, src)
            return dst
        except MediaError:
            tmp.unlink(missing_ok=True)
            log.info("remux failed for %s (%s); transcoding to aac",
                     src.name, info.codec)

    # 2) universal fallback: aac 192k, still inside the seekable container
    dst = d / (key + ".m4a")
    if not dst.exists():
        tmp = d / (key + ".part.m4a")
        _ffmpeg(["-i", str(src), "-vn", "-c:a", "aac", "-b:a", "192k",
                 *_MUX_MP4], tmp)
        os.replace(tmp, dst)
        log.info("audio cache (aac): %s -> %s", src.name, dst.name)
    _index_note(cfg, key, src)
    return dst


# ----------------------------------------------------------------- resolve

def resolve_playable(cfg: Config, override, source) -> dict:
    """Playback resolution shared by the refine preview and the storage
    list: override > source. Directly playable files pass through;
    video/exotic audio resolve to their cache when it exists, else the
    entry is PENDING (first play extracts via /api/audio/prepare)."""
    for cand in (override, source):
        if not (cand and Path(cand).is_file()):
            continue
        p = Path(cand)
        if not needs_extraction(p):
            return {"resolved": str(p), "pending": False,
                    "pending_path": None, "missing": None}
        cache = existing_cache(cfg, p)
        if cache:
            return {"resolved": str(cache), "pending": False,
                    "pending_path": None, "missing": None}
        return {"resolved": None, "pending": True,
                "pending_path": str(p), "missing": None}
    return {"resolved": None, "pending": False,
            "pending_path": None, "missing": override or source}


# ------------------------------------------------------------ cache manager

def list_cache(cfg: Config) -> dict:
    """Cache entries for the settings-card manager. Orphaned files
    without an index entry still list (keyed by file name)."""
    d = audio_cache_dir(cfg)
    idx = _index_load(cfg)
    entries = []
    total = 0
    if d.is_dir():
        for p in sorted(d.glob("*")):
            if p.name == "index.json" or ".part" in p.name:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            total += st.st_size
            src = (idx.get(p.name.split(".")[0]) or {}).get("source")
            entries.append({
                "file": p.name,
                "source": src,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(
                    st.st_mtime).isoformat(timespec="seconds"),
            })
    return {"dir": str(d), "entries": entries, "total_bytes": total}


def delete_cache(cfg: Config, files: list[str]) -> dict:
    """Remove specific cache files. Names must be PLAIN file names inside
    the cache dir -- this must never become an arbitrary-file deleter."""
    d = audio_cache_dir(cfg).resolve()
    removed = 0
    for n in files:
        try:
            p = (audio_cache_dir(cfg) / n).resolve()
        except OSError:
            continue
        if p.parent != d or p.name == "index.json" or not p.is_file():
            continue
        p.unlink(missing_ok=True)
        removed += 1
    _index_gc(cfg)
    out = list_cache(cfg)
    out["removed"] = removed
    return out


def clear_cache(cfg: Config) -> dict:
    """Remove every cached track (and the index -- the whole cache is
    derived data, nothing here is worth keeping)."""
    d = audio_cache_dir(cfg)
    removed = 0
    if d.is_dir():
        for p in d.iterdir():
            if not p.is_file() or p.name == "index.json":
                continue
            p.unlink(missing_ok=True)
            removed += 1
        (d / "index.json").unlink(missing_ok=True)
    out = list_cache(cfg)
    out["removed"] = removed
    return out
