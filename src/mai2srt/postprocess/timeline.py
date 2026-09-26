"""Timestamp passes (PLAN 5.5/5.6, port of trans-jimaku-web MAI profile):

* outward expansion +-0.25 s -- MAI boundaries are systematically shrunk
  (~80% of lines start late / end early); each shared boundary hands at
  most half of its spare gap (beyond the min gap) to each side, so
  expansion never creates an overlap; first start clamps at 0, last end
  at the audio duration;
* final formatting -- overlap fix, min spacing 100 ms, min duration
  (target 1.2 s / absolute 0.5 s). The MAI profile skips the ElevenLabs
  pull-forward, the max-duration trim and the dialogue cap entirely.
"""
from __future__ import annotations

from dataclasses import dataclass

from .dialogue import SubEntry


@dataclass
class TimelineParams:
    mai_expand_s: float = 0.25
    min_gap_s: float = 0.1
    min_duration_s: float = 1.2
    min_duration_abs_s: float = 0.5


def expand_outward(
    entries: list[SubEntry], p: TimelineParams, audio_duration_s: float | None
) -> None:
    """In-place +-0.25 s outward expansion with gap clamping (see module doc)."""
    n = len(entries)
    if n == 0:
        return
    start_back = [p.mai_expand_s] * n
    end_fwd = [p.mai_expand_s] * n
    for i in range(n - 1):
        spare = entries[i + 1].start - entries[i].end - p.min_gap_s
        side = max(0.0, spare) / 2.0
        end_fwd[i] = min(end_fwd[i], side)
        start_back[i + 1] = min(start_back[i + 1], side)
    start_back[0] = min(start_back[0], max(0.0, entries[0].start))
    if audio_duration_s and audio_duration_s > 0:
        end_fwd[n - 1] = min(
            end_fwd[n - 1],
            max(0.0, audio_duration_s - entries[n - 1].end),
        )
    for i, entry in enumerate(entries):
        entry.start = max(0.0, entry.start - start_back[i])
        entry.end = max(entry.end + end_fwd[i], entry.start + 0.001)


def final_format(entries: list[SubEntry], p: TimelineParams) -> list[SubEntry]:
    """Overlap fix, min spacing, min duration; assigns nothing else."""
    result: list[SubEntry] = []
    for cur in entries:
        prev = result[-1] if result else None
        if prev is not None:
            raw_gap = cur.start - prev.end
            if raw_gap < -0.01:                       # overlap fix
                new_start = prev.end + 0.01
                if new_start < cur.end:
                    cur.start = new_start
            if cur.start < prev.end + p.min_gap_s:    # min spacing
                cur.start = prev.end + p.min_gap_s
        final_min = max(p.min_duration_s, p.min_duration_abs_s)
        if cur.duration < final_min:                  # min duration
            cur.end = max(cur.end, cur.start + final_min)
        result.append(cur)
    return result
