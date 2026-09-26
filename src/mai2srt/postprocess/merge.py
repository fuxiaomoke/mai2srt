"""Smart merge (PLAN 5.3, port of trans-jimaku-web mode-B merge).

Veto chain (first hit wins): speaker sets disjoint -> dialogue entry ->
merge gap over threshold -> merged duration over max -> merged text over
max chars -> merged CPS over cap (CJK 13 / other 18). A pair merges only
when every veto passes AND the benefit exceeds 5.0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..segment.rules import is_cjk
from .dialogue import SubEntry, _glue


@dataclass
class MergeParams:
    max_duration_s: float = 12.0
    max_chars: int = 60
    min_duration_s: float = 1.2
    merge_gap_threshold_s: float = 0.8
    cps_cjk: float = 13.0
    cps_other: float = 18.0
    benefit_threshold: float = 5.0


def _cps(text: str, duration: float) -> float:
    if duration <= 0:
        return 999.0
    return len(re.sub(r"\s+", "", text)) / duration


def can_merge(e1: SubEntry, e2: SubEntry, p: MergeParams) -> bool:
    # top veto: disjoint speaker sets never merge (speaker switch is a hard
    # boundary; entries lacking speaker info are not blocked)
    sa = {s for s in e1.speakers if s is not None}
    sb = {s for s in e2.speakers if s is not None}
    if sa and sb and not (sa & sb):
        return False
    if e1.is_dialogue or e2.is_dialogue:
        return False
    # (M6e sentence-final veto REVERTED per M6f user decision: punctuation
    # is not a trustworthy semantic signal across languages)
    if e2.start - e1.end > p.merge_gap_threshold_s:
        return False
    merged_duration = e2.end - e1.start
    if merged_duration > p.max_duration_s:
        return False
    merged_text = _glue(e1.text, e2.text)
    if len(merged_text) > p.max_chars:
        return False
    max_cps = p.cps_cjk if is_cjk(merged_text) else p.cps_other
    if _cps(merged_text, merged_duration) > max_cps:
        return False
    return True


def merge_benefit(e1: SubEntry, e2: SubEntry, p: MergeParams) -> float:
    score = 0.0
    if e1.duration < p.min_duration_s:
        score += (p.min_duration_s - e1.duration) * 20
    if e2.duration < p.min_duration_s:
        score += (p.min_duration_s - e2.duration) * 20
    gap = e2.start - e1.end
    if gap < 0.3:
        score += (0.3 - gap) * 10
    elif gap < 0.5:
        score += (0.5 - gap) * 5
    if len(e1.text) < 5:
        score += 5
    if len(e2.text) < 5:
        score += 5
    return score


def _merge_two(e1: SubEntry, e2: SubEntry) -> SubEntry:
    return SubEntry(
        e1.start,
        e2.end,
        _glue(e1.text, e2.text),
        e1.speakers | e2.speakers,
        is_dialogue=False,
        words=e1.words + e2.words,
    )


def smart_merge(entries: list[SubEntry], p: MergeParams) -> list[SubEntry]:
    """Greedy pairwise merge over neighbours; benefit must clear the bar."""
    out: list[SubEntry] = []
    i = 0
    while i < len(entries):
        cur = entries[i]
        if i + 1 < len(entries):
            nxt = entries[i + 1]
            if can_merge(cur, nxt, p) and merge_benefit(cur, nxt, p) > p.benefit_threshold:
                out.append(_merge_two(cur, nxt))
                i += 2
                continue
        out.append(cur)
        i += 1
    return out
