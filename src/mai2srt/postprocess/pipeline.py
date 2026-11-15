"""Post-processing pipeline (PLAN 5 + the 2026-11-15 fuse): dialogue ->
smart merge -> safety-net split -> +-0.25 s expansion -> final formatting.

Input: M2 ``Segment`` list (single-speaker, word-anchored, within limits).
Output: ``SubEntry`` list ready for SRT serialization. The MAI profile is
encoded here: no pull-forward, no max-duration trim, no dialogue cap.

Two phases run BEFORE the ones above (2026-11-15, A/B/合 audit):

* ``_silence_split`` -- a line whose words span a >= 3 s silence is cut there.
  The segmenter sees text only, so nothing else stops it from swallowing a
  pause: measured on a 55-min source, 45 of 436 lines straddled a >= 3 s
  silence. Statistics behind the 3 s threshold (trans-jimaku-web's audit, ~3000
  hand-timed silences): 3-5 s cut by hand 95.6% of the time, 5-8 s 99.1%,
  >= 8 s 100%. 1.5-3 s stays with the model (it is a genuine grey zone).
* ``_fold_tiny`` -- a one-character line (「の」「ト」「ル」「？」) is a
  diarization/timestamp artifact, not a subtitle: it joins the temporally
  closer neighbour when a short gap allows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..messages import tr
from ..segment.pipeline import Segment
from ..segment.rules import SegmentParams, is_over_limit, join_words, split_fallback
from .dialogue import SubEntry, _glue, detect_dialogue
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
    # dialogue (MAI interleave: X Y X / X Y Z X)
    dialogue_gap_tolerance_s: float = 0.2
    dialogue_max_speakers: int = 3
    dialogue_max_duration_s: float = 16.0
    dialogue_max_chars: int = 100
    # fuse: a line may not swallow a silence this long
    silence_split_s: float = 3.0
    # fuse: a line this short is an artifact, not a subtitle
    fragment_min_chars: int = 2
    fragment_join_gap_s: float = 1.0
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
    lang: str = "zh",
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

    entries = _silence_split(entries, p, emit, lang)
    entries = _fold_tiny(entries, p, emit, lang)
    entries = detect_dialogue(
        entries,
        tolerance_s=p.dialogue_gap_tolerance_s,
        max_speakers=p.dialogue_max_speakers,
        max_duration_s=p.dialogue_max_duration_s,
        max_chars=p.dialogue_max_chars,
        log=emit,
    )
    entries = smart_merge(entries, p)
    entries = _safety_net_split(entries, p, split_params, emit)

    expand_outward(entries, p, audio_duration_s)
    entries = final_format(entries, p, audio_duration_s)
    return entries


def _silence_split(
    entries: list[SubEntry], p: PostprocessParams, emit: Log, lang: str = "zh"
) -> list[SubEntry]:
    """Cut lines at internal silences >= ``silence_split_s`` (2026-11-15).

    Deterministic on purpose: the model only ever gets pause information as a
    hint, so this is the hard guarantee that no subtitle lingers over dead air.
    Dialogue lines are exempt (their shape is speaker structure, not silence
    structure), and so are single-word lines (nothing to cut).
    """
    if p.silence_split_s <= 0:
        return entries
    out: list[SubEntry] = []
    cut = 0
    for e in entries:
        words = e.words
        if e.is_dialogue or len(words) < 2:
            out.append(e)
            continue
        parts: list[list] = []
        start = 0
        for k in range(1, len(words)):
            if words[k].start - words[k - 1].end >= p.silence_split_s:
                parts.append(words[start:k])
                start = k
        parts.append(words[start:])
        if len(parts) == 1:
            out.append(e)
            continue
        cut += 1
        out.extend(
            SubEntry(
                start=w[0].start,
                end=w[-1].end,
                text=join_words(w),
                speakers={x.speaker for x in w},
                words=list(w),
            )
            for w in parts
        )
    if cut:
        emit(tr(lang, "run.silence_split", cut=cut,
                before=len(entries), after=len(out)))
    return out


def _fold_tiny(
    entries: list[SubEntry], p: PostprocessParams, emit: Log, lang: str = "zh"
) -> list[SubEntry]:
    """Fold one-character lines into the closer neighbour (2026-11-15).

    Any such line is an artifact: MAI's word-level diarization splits inside a
    phrase, so 「の」「ト」「ル」 arrive as their own runs with their own
    ~0.08 s word. They are folded regardless of which phase produced them --
    the alternative is a subtitle line showing a single character.
    """
    if p.fragment_min_chars <= 1:
        return entries
    out: list[SubEntry] = []
    folded = 0
    i = 0
    while i < len(entries):
        e = entries[i]
        if e.is_dialogue or len(e.text.strip()) >= p.fragment_min_chars:
            out.append(e)
            i += 1
            continue
        left = out[-1] if out else None
        right = entries[i + 1] if i + 1 < len(entries) else None
        left_gap = (e.start - left.end) if left is not None else None
        right_gap = (right.start - e.end) if right is not None else None
        if (left is not None and not left.is_dialogue and left_gap is not None
                and left_gap <= p.fragment_join_gap_s
                and (right_gap is None or left_gap <= right_gap)):
            left.end = max(left.end, e.end)
            left.text = _glue(left.text, e.text)
            left.words = left.words + e.words
            left.speakers |= e.speakers
            folded += 1
            i += 1
            continue
        if (right is not None and not right.is_dialogue
                and right_gap is not None
                and right_gap <= p.fragment_join_gap_s):
            right.start = min(right.start, e.start)
            right.text = _glue(e.text, right.text)
            right.words = e.words + right.words
            right.speakers |= e.speakers
            folded += 1
            i += 1
            continue
        out.append(e)
        i += 1
    if folded:
        emit(tr(lang, "run.tiny_folded", n=folded))
    return out


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
