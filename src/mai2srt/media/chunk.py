"""Silence-aware, BALANCED chunking for media longer than the upload cap.

Two things make this more than a "cut every 29 minutes" loop:

1. The pause detection adapts to the material (a pause is ~30 dB below the
   loudest moment, not a fixed -40 dBFS), and it reports the pauses as
   INTERVALS. A fixed absolute line silently failed on noisy or normalised
   recordings -- zero pauses found -- which turned every cut into a hard cut
   and produced the residual stub of issue #5.

2. Cuts are balanced and snapped into real pauses. The old planner filled to
   the cap and appended whatever was left, so a source a few seconds past a
   29-minute multiple ended with a 2-6 s piece sitting in the media's quiet
   tail; the server answers such a piece with HTTP 422 "No speech was found
   in this audio". Even splitting -- n = ceil(D / cap) pieces of ~D/n -- makes
   that structurally impossible: every piece is at least cap/2 = 14.5 min.

A cut lands in the MIDDLE of a pause, so both sides keep a guard band (mp3
frame granularity is 26 ms; cutting at the moment speech resumes risks
clipping the first word).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import (
    CHUNK_TARGET_SECONDS,
    CUT_MIN_GAP_S,
    MIN_CHUNK_S,
    SILENCE_DB_MAX,
    SILENCE_DB_MIN,
    SILENCE_MIN_S,
    SILENCE_NOISE_DB,
    SILENCE_PEAK_FLOOR_DB,
    SILENCE_REL_DB,
    SILENCE_RETRY_DB,
    SILENCE_SANITY_COVERAGE,
    SNAP_WINDOW_RATIO,
)
from .compress import mp3_bitrate_bps
from .probe import MediaError, MediaInfo, resolve_tool, run_tool

log = logging.getLogger("mai2srt.media")

_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_END_RE = re.compile(
    r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")
_PEAK_RE = re.compile(r"max_volume:\s*(-?[0-9.]+)\s*dB")


# ---------------------------------------------------------------------------
# what the media sounds like
# ---------------------------------------------------------------------------

@dataclass
class QuietMap:
    """Where the media is quiet, and how far that answer can be trusted.

    ``gaps`` are (start_s, end_s) pauses. ``trusted`` is False when the scan
    contradicted itself (nothing quiet, then everything quiet): the map is then
    useless and the caller must fall back to evenly spaced hard cuts -- never
    to "the file has no speech", which is what ``no_signal`` alone means.
    """

    gaps: list[tuple[float, float]] = field(default_factory=list)
    peak_db: float | None = None
    threshold_db: float | None = None
    duration_s: float = 0.0
    retried: bool = False
    trusted: bool = True
    #: nothing but silence in the file (peak at the floor): nothing to send
    no_signal: bool = False

    @property
    def coverage(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return sum(e - s for s, e in self.gaps) / self.duration_s

    def cut_points(self, min_gap_s: float = CUT_MIN_GAP_S) -> list[float]:
        """Midpoints of the pauses long enough to cut inside."""
        return [(s + e) / 2.0 for s, e in self.gaps if e - s >= min_gap_s]

    def quiet_seconds(self, start_s: float, end_s: float) -> float:
        """Quiet time inside [start_s, end_s] -- lets the caller judge a piece."""
        total = 0.0
        for s, e in self.gaps:
            lo, hi = max(s, start_s), min(e, end_s)
            if hi > lo:
                total += hi - lo
        return total


def measure_peak_db(src: Path) -> float | None:
    """Loudest sample of the audio stream, in dBFS (None = no audio)."""
    ffmpeg = resolve_tool("ffmpeg")
    r = run_tool(
        [ffmpeg, "-hide_banner", "-i", str(src), "-vn",
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=7200,
    )
    m = _PEAK_RE.search(r.stderr or "")
    if not m:
        log.warning("volumedetect found no max_volume (rc=%s): %s",
                    r.returncode, (r.stderr or "")[-200:])
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _intervals(stderr: str, duration_s: float) -> list[tuple[float, float]]:
    """Pair silencedetect's start/end lines into (start, end) pauses."""
    gaps: list[tuple[float, float]] = []
    pending: float | None = None
    for line in (stderr or "").splitlines():
        m = _START_RE.search(line)
        if m:
            pending = max(0.0, float(m.group(1)))
            continue
        m = _END_RE.search(line)
        if m:
            end = float(m.group(1))
            start = (pending if pending is not None
                     else end - float(m.group(2)))
            if end > start:
                gaps.append((max(0.0, start), end))
            pending = None
    # ffmpeg closes a silence that runs to EOF at the duration; older builds
    # may leave it open, so close it here rather than lose the tail
    if pending is not None and duration_s > pending:
        gaps.append((pending, duration_s))
    return gaps


def _scan_quiet(src: Path, threshold_db: float,
                min_s: float, duration_s: float) -> list[tuple[float, float]]:
    ffmpeg = resolve_tool("ffmpeg")
    # -vn: the scan needs AUDIO only. Without it a video source decodes
    # its whole picture stream too -- a 69-min video spent 11.5 min in
    # this scan (2026-10-13); audio-only decode finishes in seconds.
    r = run_tool(
        [ffmpeg, "-hide_banner", "-i", str(src), "-vn", "-af",
         f"silencedetect=noise={threshold_db:.1f}dB:d={min_s}",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=7200,
    )
    gaps = _intervals(r.stderr or "", duration_s)
    if not gaps and r.returncode != 0:
        raise MediaError(
            f"silencedetect failed (rc={r.returncode}): "
            f"{(r.stderr or '')[-300:]}",
            code="err.scan_failed", rc=r.returncode)
    log.info("silencedetect @ %.1f dB -> %d pause(s)", threshold_db, len(gaps))
    return gaps


def _coverage(gaps: list[tuple[float, float]], duration_s: float) -> float:
    if duration_s <= 0:
        return 0.0
    return sum(e - s for s, e in gaps) / duration_s


def detect_quiet(src: Path, duration_s: float,
                 min_s: float = SILENCE_MIN_S) -> QuietMap:
    """Measure the material, then find its pauses (at most two decode passes).

    The silence line is derived from the measured peak and sanity-checked: a
    scan that finds nothing, or that calls almost the whole file quiet, says
    more about the line than about the audio, so one corrective re-scan runs
    before giving up. Giving up is not a failure -- balanced hard cuts are
    still perfectly valid chunks; it only costs the pause-aligned boundaries.
    """
    peak = measure_peak_db(src)
    if peak is not None and peak <= SILENCE_PEAK_FLOOR_DB:
        log.info("peak %.1f dB is at the floor: the file has no signal", peak)
        return QuietMap(peak_db=peak, duration_s=duration_s, no_signal=True)

    base = (peak - SILENCE_REL_DB) if peak is not None else SILENCE_NOISE_DB
    threshold = min(max(base, SILENCE_DB_MIN), SILENCE_DB_MAX)
    gaps = _scan_quiet(src, threshold, min_s, duration_s)
    cov = _coverage(gaps, duration_s)

    if gaps and cov <= SILENCE_SANITY_COVERAGE:
        log.info("quiet map: peak=%s threshold=%.1f dB gaps=%d coverage=%.1f%%",
                 f"{peak:.1f}" if peak is not None else "n/a",
                 threshold, len(gaps), cov * 100)
        return QuietMap(gaps, peak, threshold, duration_s)

    # 0 pauses -> the line is too low; absurd coverage -> it is too high
    retry = threshold + SILENCE_RETRY_DB if not gaps else threshold - SILENCE_RETRY_DB
    retry = min(max(retry, SILENCE_DB_MIN), SILENCE_DB_MAX)
    log.info("quiet map rejected (%d gaps, %.1f%% quiet); re-scanning @ %.1f dB",
             len(gaps), cov * 100, retry)
    gaps2 = _scan_quiet(src, retry, min_s, duration_s)
    cov2 = _coverage(gaps2, duration_s)
    if gaps2 and cov2 <= SILENCE_SANITY_COVERAGE:
        log.info("quiet map (retry): threshold=%.1f dB gaps=%d coverage=%.1f%%",
                 retry, len(gaps2), cov2 * 100)
        return QuietMap(gaps2, peak, retry, duration_s, retried=True)
    log.warning("no believable pause map (%.1f%% quiet at %.1f dB); "
                "falling back to evenly spaced hard cuts",
                cov2 * 100, retry)
    return QuietMap([], peak, retry, duration_s,
                    retried=True, trusted=False)


# ---------------------------------------------------------------------------
# chunk planning
# ---------------------------------------------------------------------------

@dataclass
class ChunkPlan:
    pieces: list[tuple[float, float]]   # (start_s, end_s) in source time

    @property
    def offsets(self) -> list[float]:
        return [s for s, _ in self.pieces]


def plan_chunks(duration_s: float, cut_points: list[float],
                target_s: float = CHUNK_TARGET_SECONDS,
                min_chunk_s: float = MIN_CHUNK_S,
                allow_extra: bool = True) -> ChunkPlan:
    """Split into the FEWEST balanced pieces, snapping each cut into a pause.

    Invariants: the pieces are contiguous (they cover [0, duration] with no
    gap), none exceeds ``target_s``, none is shorter than ``min_chunk_s``
    (unless the whole media is), and the even split means no piece is shorter
    than target/2 -- a residual stub cannot be produced (issue #5).

    ``allow_extra`` is the "one more piece buys a snapping window" option: when
    the even split leaves no room for a cut to move (a source that is an exact
    multiple of the cap has none), one extra piece is tried and kept only if it
    turns hard cuts into pause-aligned ones.
    """
    if duration_s <= target_s:
        return ChunkPlan([(0.0, duration_s)])
    pts = sorted(cut_points)
    n = max(1, int(math.ceil(duration_s / target_s)))
    best = _place(duration_s, pts, target_s, min_chunk_s, n)
    if best is not None and best[1] == 0:
        return ChunkPlan(best[0])
    if allow_extra and duration_s / (n + 1) >= min_chunk_s:
        extra = _place(duration_s, pts, target_s, min_chunk_s, n + 1)
        if extra is not None and (best is None or extra[1] < best[1]):
            log.info("plan: %d -> %d pieces to cut inside pauses "
                     "(%d hard cut(s) instead of %d)",
                     n, n + 1, extra[1], best[1] if best else -1)
            return ChunkPlan(extra[0])
    if best is not None:
        log.info("plan: %d piece(s), %d hard cut(s)", n, best[1])
        return ChunkPlan(best[0])
    # pathological (a target below min_chunk_s, or a caller passing its own):
    # even spacing always satisfies the caps, it just ignores pauses
    log.warning("plan: no valid placement, using plain even splits")
    return ChunkPlan(_even_split(duration_s, target_s))


def _place(duration_s: float, pts: list[float], target_s: float,
           min_chunk_s: float, n: int) -> tuple[list[tuple[float, float]], int] | None:
    """Try ``n`` pieces; returns (pieces, hard_cut_count) or None if impossible."""
    ideal = duration_s / n
    window = ideal * SNAP_WINDOW_RATIO
    pieces: list[tuple[float, float]] = []
    hard = 0
    prev = 0.0
    for k in range(1, n):
        b = k * ideal
        # the remaining pieces must still fit under the cap, which is what
        # keeps the LAST one from being pushed over it by a late snap
        lo = max(prev + min_chunk_s, b - window, duration_s - (n - k) * target_s)
        hi = min(b + window, prev + target_s, duration_s - min_chunk_s)
        if hi < lo:
            return None
        cand = [p for p in pts if lo < p <= hi]
        if cand:
            cut = min(cand, key=lambda p: abs(p - b))
        else:
            cut = min(max(b, lo), hi)
            hard += 1
        pieces.append((prev, cut))
        prev = cut
    pieces.append((prev, duration_s))
    if duration_s - prev > target_s + 1e-6:
        return None
    return pieces, hard


def _even_split(duration_s: float,
                target_s: float) -> list[tuple[float, float]]:
    n = max(1, int(math.ceil(duration_s / target_s)))
    step = duration_s / n
    return [(i * step, (i + 1) * step) for i in range(n)]


def cut_chunk(src: Path, dst: Path, start_s: float, end_s: float) -> Path:
    """Encode one chunk to mp3 sized for the upload budget."""
    from .compress import encode_mp3  # local import avoids cycle at module load
    bitrate = mp3_bitrate_bps(end_s - start_s)
    return encode_mp3(src, dst, bitrate, start_s=start_s, end_s=end_s)


# ---------------------------------------------------------------------------
# preparation
# ---------------------------------------------------------------------------

def _fmt_span(seconds: float, lang: str = "zh") -> str:
    """Human length: seconds under a minute, else minutes (in the user's
    language -- these strings go straight to the Transcribe page)."""
    from ..messages import tr
    if seconds < 60:
        return tr(lang, "span.secs", n=seconds)
    return tr(lang, "span.mins", n=seconds / 60.0)


def prepare(audio: Path, info: MediaInfo, workdir: Path,
            on_progress=None, lang: str = "zh") -> list[tuple[Path, float]]:
    """Turn any audio into upload-ready (file, offset) pairs.

    ``on_progress`` (optional) receives short human-readable progress
    lines (silence scan -> chunk plan -> per-chunk encode). Preparation
    of a long video runs for minutes; without these lines the UI sat on
    a static "prepare" stage and looked wedged (2026-10-13).

    The user-facing lines stay short and plain; the numbers behind them go to
    the log (which the UI shows on demand), so a bug report can be filled in
    without reading a wall of dB figures while transcribing.

    - fits the 25 MiB raw limit in one piece (mp3/wav) -> as-is
    - longer than the diarization-safe duration cap -> balanced, pause-aligned
      chunks, each encoded to mp3
    - wrong format / over the limit -> single mp3 encode

    Limits are on RAW bytes: the site's own picker gates the file size,
    and a 23 MiB file (~30.7 MiB base64) transcribed fine live.

    Chunking triggers at the DURATION cap (30 min), not the old
    floor-bitrate fit (~52 min): 50-min chunks drew a deterministic
    stream 403 on 2026-10-13 (see config.MAX_SINGLE_UPLOAD_SECONDS).
    """
    from ..config import (MAX_AUDIO_SECONDS, MAX_UPLOAD_BYTES,
                          MAX_SINGLE_UPLOAD_SECONDS)
    from ..messages import tr
    p = on_progress or (lambda msg: None)
    workdir.mkdir(parents=True, exist_ok=True)
    stem = audio.stem[:40]

    # chunk past the diarization-safe duration cap or the site's gate --
    # both sit well below the old floor-bitrate overflow point
    if (info.duration_s > MAX_AUDIO_SECONDS
            or info.duration_s > MAX_SINGLE_UPLOAD_SECONDS):
        p(tr(lang, "media.analyzing"))
        quiet = detect_quiet(audio, info.duration_s)
        if quiet.no_signal:
            # nothing to transcribe: fail now instead of uploading silence
            raise MediaError(tr(lang, "run.no_speech"))
        if not quiet.trusted:
            p(tr(lang, "media.no_pauses"))
        plan = plan_chunks(info.duration_s, quiet.cut_points())
        n = len(plan.pieces)
        log.info("quiet map: peak=%s threshold=%s retried=%s trusted=%s "
                 "gaps=%d cut_candidates=%d coverage=%.1f%%",
                 quiet.peak_db, quiet.threshold_db, quiet.retried,
                 quiet.trusted, len(quiet.gaps), len(quiet.cut_points()),
                 quiet.coverage * 100)
        log.info("plan: %d piece(s), ideal=%.1fs, pieces=%s", n,
                 info.duration_s / n,
                 ", ".join("%.1f-%.1fs" % (s, e) for s, e in plan.pieces))
        p(tr(lang, "media.plan", n=n,
             span=_fmt_span(info.duration_s / n, lang)))
        out: list[tuple[Path, float]] = []
        for i, (s, e) in enumerate(plan.pieces):
            p(tr(lang, "media.encode", i=i + 1, n=n,
                 span=_fmt_span(e - s, lang)))
            dst = workdir / f"{stem}.chunk{i:02d}.mp3"
            cut_chunk(audio, dst, s, e)
            size = dst.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                raise MediaError(
                    f"chunk {i} is {size / 1024 / 1024:.1f} MiB after "
                    "compression; lower CHUNK_TARGET_SECONDS",
                    code="err.chunk_too_big", i=i, size=size / 1024 / 1024)
            out.append((dst, s))
            p(tr(lang, "media.encoded", i=i + 1, n=n, size=size / 1024 / 1024))
            speech = (e - s) - quiet.quiet_seconds(s, e)
            log.info("chunk %d: %.1fs..%.1fs -> %s (%.1f MiB, speech %.1f%%)",
                     i, s, e, dst.name, size / 1024 / 1024,
                     100.0 * speech / max(e - s, 1e-6))
        return out

    if info.mime and info.size_bytes <= MAX_UPLOAD_BYTES:
        p(tr(lang, "media.passthrough"))
        return [(audio, 0.0)]

    p(tr(lang, "media.compressing"))
    dst = workdir / f"{stem}.upload.mp3"
    from .compress import compress_for_upload
    compress_for_upload(audio, dst, info.duration_s)
    return [(dst, 0.0)]
