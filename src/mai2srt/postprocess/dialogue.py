"""Line-level dialogue detection: scanline + Union-Find interval clustering.

Design (PLAN 5.2, adopted from the user's Perplexity research, script archived
at ``_research/ref/srt_overlap_merge.py``): entries sorted by start; a
scanline keeps the still-active entries; every pair (new, active) whose
intervals overlap WITHIN ``dialogue_gap_tolerance_s`` (MAI serialises
simultaneous speech as adjacent non-overlapping utterances, gaps 0.02-0.1 s
-- spike-measured) AND whose speakers DIFFER is unioned. Each connected
component becomes one dialogue group: min(start)/max(end) timeline, per-
speaker fragments joined in first-appearance order as ``- A\\n- B``.

Differences from the reference script (user decisions):
* union guard: only different-speaker pairs are unioned (same-speaker
  overlap must not glue two solo lines of one person into a dialogue);
* after clustering, groups holding MORE than ``dialogue_max_speakers``
  (default 2) distinct speakers are NOT merged -- their lines stay
  separate and a log line is emitted;
* dialogue duration has no cap (a >20 s group gets a log note only).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..segment.rules import is_cjk as _is_cjk


class UnionFind:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


@dataclass
class SubEntry:
    """One subtitle line through post-processing (timestamps are mutable)."""

    start: float
    end: float
    text: str
    speakers: set = field(default_factory=set)   # set[str | None]
    is_dialogue: bool = False
    words: list = field(default_factory=list)    # word refs (dialogue groups span entries)

    @property
    def duration(self) -> float:
        return self.end - self.start


def _glue(prev_text: str, next_text: str) -> str:
    sep = "" if (_is_cjk(prev_text) and _is_cjk(next_text)) else " "
    return prev_text + sep + next_text


def detect_dialogue(
    entries: list[SubEntry],
    tolerance_s: float = 0.2,
    max_speakers: int = 2,
    long_warn_s: float = 20.0,
    log=None,
) -> list[SubEntry]:
    """Cluster near-overlapping different-speaker entries into dialogue lines.

    Returns a new list; solo entries pass through untouched (never rewritten,
    never split). ``None`` speaker entries never join a dialogue (unknown
    speaker = no evidence of a second voice).
    """
    emit = log or (lambda m: None)
    n = len(entries)
    if n < 2:
        return list(entries)

    uf = UnionFind(n)
    active: list[int] = []
    for i in range(n):
        cur = entries[i]
        still: list[int] = []
        for j in active:
            # entry j is still live when it ends within tolerance of cur.start
            if entries[j].end + tolerance_s >= cur.start:
                still.append(j)
                if _pairable(entries[j], cur):
                    uf.union(i, j)
        active = still + [i]
    # note: entries arrive sorted by start (pipeline guarantees); the scanline
    # only needs end-forward reachability, which holds under sort order.

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)

    out: list[SubEntry] = []
    for idxs in groups.values():
        members = [entries[i] for i in sorted(idxs, key=lambda i: entries[i].start)]
        speakers = set()
        for m in members:
            speakers.update(m.speakers)
        distinct = {s for s in speakers if s is not None}
        if len(idxs) == 1 or len(distinct) < 2:
            out.extend(members)
            continue
        if len(distinct) > max_speakers:
            emit("dialogue group %.1fs-%.1fs has %d speakers (> %d cap): "
                 "lines kept separate"
                 % (members[0].start, members[-1].end, len(distinct), max_speakers))
            out.extend(members)
            continue

        order: list = []            # first-appearance speaker order
        frags: dict = {}            # speaker -> list[str]
        region_words: list = []
        for m in members:
            # M2 guarantees single-speaker entries; take that one speaker
            sp0 = min(m.speakers) if m.speakers else None
            if sp0 is None:
                continue            # unknown-speaker line cannot join a dialogue
            if sp0 not in frags:
                frags[sp0] = []
                order.append(sp0)
            frags[sp0].append(m.text)
            region_words.extend(m.words)

        text = "\n".join(
            "- " + _glue_seq(frags[sp]) for sp in order if frags[sp]
        )
        start = min(m.start for m in members)
        end = max(m.end for m in members)
        if end - start > long_warn_s:
            emit("dialogue group %.1fs-%.1fs is long (%.1fs): kept, review "
                 "advised" % (start, end, end - start))
        emit("dialogue merged: %d lines, %d speakers | %s"
             % (len(members), len(order), text.replace("\n", " / ")[:40]))
        out.append(SubEntry(
            start, end, text, set(distinct), is_dialogue=True, words=region_words,
        ))

    out.sort(key=lambda e: e.start)
    return out


def _pairable(a: SubEntry, b: SubEntry) -> bool:
    """Different-speaker check for the union guard (None never pairable)."""
    sa = {s for s in a.speakers if s is not None}
    sb = {s for s in b.speakers if s is not None}
    return bool(sa) and bool(sb) and not (sa & sb)


def _glue_seq(parts: list[str]) -> str:
    out = ""
    for t in parts:
        out = t if not out else _glue(out, t)
    return out
