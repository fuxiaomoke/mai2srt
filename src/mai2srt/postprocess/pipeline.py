"""Post-processing pipeline (PLAN 5): dialogue -> smart merge -> safety-net
split -> +-0.25 s expansion -> final formatting.

Input: M2 ``Segment`` list (single-speaker, word-anchored, within limits).
Output: ``SubEntry`` list ready for SRT serialization. The MAI profile is
encoded here: no pull-forward, no max-duration trim, no dialogue cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..segment.pipeline import Segment
from ..segment.rules import SegmentParams, is_over_limit, join_words, split_fallback
from .dialogue import SubEntry, detect_dialogue
from .merge import smart_merge
from .timeline import expand_outward, final_format

Log = Callable[[str], None]


@dataclass
class PostprocessParams:
    # shared limits (PLAN 5 defaults)
    max_duration_s: float = 12.0
    max_chars: int = 60
    min_duration_s: float = 1.2
    # smart merge
    merge_gap_threshold_s: float = 0.8
    cps_cjk: float = 13.0
    cps_other: float = 18.0
    benefit_threshold: float = 5.0
    # dialogue
    dialogue_gap_tolerance_s: float = 0.2
    dialogue_max_speakers: int = 2
    dialogue_long_warn_s: float = 20.0
    # timeline
    mai_expand_s: float = 0.25
    min_gap_s: float = 0.1
    min_duration_abs_s: float = 0.5


def process(
    segments: list[Segment],
    params: PostprocessParams | None = None,
    audio_duration_s: float | None = None,
    split_params: SegmentParams | None = None,
    log: Log | None = None,
) -> list[SubEntry]:
    """Run the full MAI post-processing profile over segmented lines."""
    p = params or PostprocessParams()
    emit = log or (lambda m: None)

    entries = [
        SubEntry(
            start=s.start,
            end=s.end,
            text=s.text,
            speakers={w.speaker for w in s.words},
            words=list(s.words),
        )
        for s in segments
    ]

    entries = detect_dialogue(
        entries,
        tolerance_s=p.dialogue_gap_tolerance_s,
        max_speakers=p.dialogue_max_speakers,
        long_warn_s=p.dialogue_long_warn_s,
        log=emit,
    )
    entries = smart_merge(entries, p)
    entries = _safety_net_split(entries, p, split_params, emit)

    expand_outward(entries, p, audio_duration_s)
    entries = final_format(entries, p)
    return entries


def _safety_net_split(
    entries: list[SubEntry],
    p: PostprocessParams,
    split_params: SegmentParams | None,
    emit: Log,
) -> list[SubEntry]:
    """M2 already guarantees within-limit lines; this only catches escapes
    (e.g. fallback depth cap) -- dialogue lines are exempt (no cap by design).
    """
    sp = split_params or SegmentParams(
        max_duration_s=p.max_duration_s, max_chars=p.max_chars
    )
    out: list[SubEntry] = []
    for e in entries:
        over = e.duration > p.max_duration_s or len(e.text) > p.max_chars
        if over and not e.is_dialogue and len(e.words) > 1:
            pieces = split_fallback(e.words, sp)
            if len(pieces) > 1:
                emit("safety-net split: %.1fs/%dch line -> %d pieces"
                     % (e.duration, len(e.text), len(pieces)))
                out.extend(
                    SubEntry(
                        start=pc[0].start,
                        end=pc[-1].end,
                        text=join_words(pc),
                        speakers={w.speaker for w in pc},
                        words=pc,
                    )
                    for pc in pieces
                )
                continue
        out.append(e)
    return out
