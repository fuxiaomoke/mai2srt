"""Silence-aware chunking for audio longer than the playground limit.

Plan chunks at silence points (ffmpeg silencedetect) so cuts rarely split
sentences; encode each chunk straight to mp3 (chunking and compression in
one pass). Fallback: hard cut at the target boundary.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    CHUNK_TARGET_SECONDS,
    SILENCE_MIN_S,
    SILENCE_NOISE_DB,
)
from .compress import mp3_bitrate_bps
from .probe import MediaError, MediaInfo, resolve_tool, run_tool

log = logging.getLogger("mai2srt.media")

_SILENCE_END_RE = re.compile(
    r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")


@dataclass
class ChunkPlan:
    pieces: list[tuple[float, float]]   # (start_s, end_s) in source time

    @property
    def offsets(self) -> list[float]:
        return [s for s, _ in self.pieces]


def detect_silences(src: Path) -> list[float]:
    """Timestamps (seconds) where a >=SILENCE_MIN_S silence ENDS."""
    ffmpeg = resolve_tool("ffmpeg")
    # -vn: the scan needs AUDIO only. Without it a video source decodes
    # its whole picture stream too -- a 69-min video spent 11.5 min in
    # this scan (2026-10-13); audio-only decode finishes in seconds.
    r = run_tool(
        [ffmpeg, "-hide_banner", "-i", str(src), "-vn", "-af",
         f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_S}",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=7200,
    )
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(r.stderr or "")]
    if not ends and r.returncode != 0:
        raise MediaError(
            f"silencedetect failed (rc={r.returncode}): "
            f"{(r.stderr or '')[-300:]}")
    log.info("detected %d silence points", len(ends))
    return ends


def plan_chunks(duration_s: float, silences: list[float],
                target_s: float = CHUNK_TARGET_SECONDS) -> ChunkPlan:
    """Greedily take the latest silence at/before each target boundary."""
    if duration_s <= target_s:
        return ChunkPlan([(0.0, duration_s)])
    pieces: list[tuple[float, float]] = []
    start = 0.0
    while duration_s - start > target_s:
        boundary = start + target_s
        window = [t for t in silences if start + 5.0 < t <= boundary]
        cut = window[-1] if window else boundary
        pieces.append((start, cut))
        start = cut
    pieces.append((start, duration_s))
    log.info("planned %d chunks: %s", len(pieces),
             ", ".join(f"{e - s:.0f}s" for s, e in pieces))
    return ChunkPlan(pieces)


def cut_chunk(src: Path, dst: Path, start_s: float, end_s: float) -> Path:
    """Encode one chunk to mp3 sized for the upload budget."""
    from .compress import encode_mp3  # local import avoids cycle at module load
    bitrate = mp3_bitrate_bps(end_s - start_s)
    return encode_mp3(src, dst, bitrate, start_s=start_s, end_s=end_s)


def prepare(audio: Path, info: MediaInfo, workdir: Path,
            on_progress=None) -> list[tuple[Path, float]]:
    """Turn any audio into upload-ready (file, offset) pairs.

    ``on_progress`` (optional) receives short human-readable progress
    lines (silence scan -> chunk plan -> per-chunk encode). Preparation
    of a long video runs for minutes; without these lines the UI sat on
    a static "prepare" stage and looked wedged (2026-10-13).

    - fits the 25 MiB raw limit in one piece (mp3/wav) -> as-is
    - longer than the diarization-safe duration cap -> silence-aware
      chunks, each encoded to mp3
    - wrong format / over the limit -> single mp3 encode

    Limits are on RAW bytes: the site's own picker gates the file size,
    and a 23 MiB file (~30.7 MiB base64) transcribed fine live.

    Chunking now triggers at the DURATION cap (30 min), not the old
    floor-bitrate fit (~52 min): 50-min chunks drew a deterministic
    stream 403 on 2026-10-13 (see config.MAX_SINGLE_UPLOAD_SECONDS).
    """
    from ..config import (MAX_AUDIO_SECONDS, MAX_UPLOAD_BYTES,
                          MAX_SINGLE_UPLOAD_SECONDS)
    p = on_progress or (lambda msg: None)
    workdir.mkdir(parents=True, exist_ok=True)
    stem = audio.stem[:40]

    # chunk past the diarization-safe duration cap or the site's gate --
    # both sit well below the old floor-bitrate overflow point
    if (info.duration_s > MAX_AUDIO_SECONDS
            or info.duration_s > MAX_SINGLE_UPLOAD_SECONDS):
        p("scanning for silence points (audio-only decode)")
        silences = detect_silences(audio)
        p("detected %d silence points" % len(silences))
        plan = plan_chunks(info.duration_s, silences)
        p("planned %d chunks: %s" % (
            len(plan.pieces),
            ", ".join("%.0f min" % ((e - s) / 60) for s, e in plan.pieces)))
        out: list[tuple[Path, float]] = []
        for i, (s, e) in enumerate(plan.pieces):
            p("encoding chunk %d/%d (%.0f min)" % (
                i + 1, len(plan.pieces), (e - s) / 60))
            dst = workdir / f"{stem}.chunk{i:02d}.mp3"
            cut_chunk(audio, dst, s, e)
            size = dst.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                raise MediaError(
                    f"chunk {i} is {size / 1024 / 1024:.1f} MiB after "
                    "compression; lower CHUNK_TARGET_SECONDS")
            out.append((dst, s))
            p("chunk %d/%d ready (%.1f MiB)" % (
                i + 1, len(plan.pieces), size / 1024 / 1024))
            log.info("chunk %d: %.1fs..%.1fs -> %s (%.1f MiB)",
                     i, s, e, dst.name, size / 1024 / 1024)
        return out

    if info.mime and info.size_bytes <= MAX_UPLOAD_BYTES:
        p("file within upload limits; sending as-is")
        return [(audio, 0.0)]

    p("compressing to mp3 for upload")
    dst = workdir / f"{stem}.upload.mp3"
    from .compress import compress_for_upload
    compress_for_upload(audio, dst, info.duration_s)
    return [(dst, 0.0)]
