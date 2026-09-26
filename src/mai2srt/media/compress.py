"""mp3 CBR compression — the only allowed lossy fallback (M0-verified).

Bitrate strategy ported from trans-jimaku-web's Opus compressor, retargeted
to mp3: derive the bitrate from the target size and the duration, clamp to
[MIN, MAX], and re-encode once with a scaled-down bitrate if the CBR encode
still overshoots (safety for container overhead).
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import (
    COMPRESS_TARGET_BYTES,
    MP3_BITRATE_MAX_BPS,
    MP3_BITRATE_MIN_BPS,
    MAX_UPLOAD_BYTES,
)
from .probe import MediaError, resolve_tool, run_tool

log = logging.getLogger("mai2srt.media")


def mp3_bitrate_bps(duration_s: float,
                    target_bytes: int = COMPRESS_TARGET_BYTES) -> int:
    """Total bitrate that fits target_bytes over duration_s, clamped."""
    bps = int(target_bytes * 8 / max(duration_s, 0.001))
    return max(MP3_BITRATE_MIN_BPS, min(bps, MP3_BITRATE_MAX_BPS))


def encode_mp3(src: Path, dst: Path, bitrate_bps: int,
               start_s: float | None = None, end_s: float | None = None) -> Path:
    """Encode (a slice of) src to mp3 CBR, preserving channels and rate."""
    ffmpeg = resolve_tool("ffmpeg")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", str(src)]
    if start_s is not None:
        cmd += ["-ss", f"{start_s:.3f}"]
    if end_s is not None:
        cmd += ["-to", f"{end_s:.3f}"]
    cmd += ["-vn", "-codec:a", "libmp3lame", "-b:a", str(bitrate_bps), str(dst)]
    r = run_tool(cmd, capture_output=True, text=True,
                 encoding="utf-8", errors="replace", timeout=7200)
    if r.returncode != 0:
        raise MediaError(f"ffmpeg mp3 encode failed: {r.stderr[-300:]}")
    return dst


def compress_for_upload(src: Path, dst: Path, duration_s: float) -> Path:
    """Compress src to an uploadable mp3, re-encoding once on overshoot.

    Guards compare RAW bytes against MAX_UPLOAD_BYTES -- the site's own
    25 MiB gate is on the file, not the base64 payload (live-verified)."""
    bitrate = mp3_bitrate_bps(duration_s)
    encode_mp3(src, dst, bitrate)
    size = dst.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        scaled = max(MP3_BITRATE_MIN_BPS,
                     int(bitrate * COMPRESS_TARGET_BYTES / size))
        log.info("encode overshot (%.1f MiB), re-encoding at %d bps",
                 size / 1024 / 1024, scaled)
        encode_mp3(src, dst, scaled)
    final = dst.stat().st_size
    if final > MAX_UPLOAD_BYTES:
        raise MediaError(
            f"compressed output still {final / 1024 / 1024:.1f} MiB "
            f"(> {MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MiB) — audio too "
            "long for a single upload; chunking should have handled this")
    log.info("compressed to %s (%.1f MiB, %d bps)",
             dst.name, final / 1024 / 1024, bitrate)
    return dst
