"""Timestamp passes (PLAN 5.5/5.6, port of trans-jimaku-web MAI profile):

* outward expansion +-0.25 s -- MAI boundaries are systematically shrunk
  (~80% of lines start late / end early); each shared boundary hands at
  most half of its spare gap (beyond the min gap) to each side, so
  expansion never creates an overlap; first start clamps at 0, last end
  at the audio duration;
* final formatting -- overlap fix, min spacing 100 ms, min duration
  (target 1.2 s / absolute 0.5 s), all granted from REAL silence only: a start
  is never moved, so one short line can no longer push the whole file late
  (2026-11-15, see final_format). The MAI profile skips the ElevenLabs
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


def final_format(
    entries: list[SubEntry],
    p: TimelineParams,
    audio_duration_s: float | None = None,
) -> list[SubEntry]:
    """Overlap fix, min spacing, min duration -- WITHOUT moving any start.

    2026-11-15 rewrite. The old version ran

        if cur.start < prev.end + min_gap:      cur.start = prev.end + min_gap
        if cur.duration < min_duration:         cur.end = cur.start + min_duration

    in that order, so propping one short line up to 1.2 s pushed the NEXT line's
    start forward, which pushed the one after it, and so on. Measured on a
    55-min three-voice source the drift reached +4.9 s by 68 s: the subtitle
    text was right, the timeline was not, and every frame showed it.

    The rules now:

    * a start is NEVER moved: the word stream is the truth about when a line
      begins. If an overlap survives the outward expansion, the PREVIOUS line's
      tail is trimmed back instead;
    * the minimum duration is granted only out of SILENCE that actually exists
      after the line -- up to the next line's start minus the minimum gap;
    * a line boxed in by its neighbours simply stays short. A honest 0.4 s line
      beats a 1.2 s lie that derails the rest of the file;
    * the absolute floor still guarantees end > start (a zero-length line is
      invisible in every player).
    """
    result: list[SubEntry] = []
    n = len(entries)
    for i, cur in enumerate(entries):
        prev = result[-1] if result else None
        if prev is not None and cur.start < prev.end + p.min_gap_s:
            # keep the start (it is the words' truth), trim the previous tail
            prev.end = max(prev.start + 0.001, cur.start - p.min_gap_s)
        room: float | None = None
        if i + 1 < n:
            room = entries[i + 1].start - p.min_gap_s
        elif audio_duration_s and audio_duration_s > 0:
            room = audio_duration_s
        want = cur.start + p.min_duration_s
        if want > cur.end and (room is None or room > cur.end):
            cur.end = want if room is None else min(want, room)
        if cur.end <= cur.start:
            cur.end = cur.start + p.min_duration_abs_s
        result.append(cur)
    return result
