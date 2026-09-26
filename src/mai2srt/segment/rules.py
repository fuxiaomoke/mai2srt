"""Deterministic segmentation rules (PLAN 4.1): three-way judgement,
short-piece merge (Pass A/C) and the punctuation-recursive fallback split.

The fallback is a port of trans-jimaku-web ``split_long_sentence``:
cut at the strongest punctuation closest to the word-count centre,
recurse on both halves; with one improvement -- when a run has no
punctuation at all, the longest inter-word pause beats a blind midpoint
cut (MAI gaps are reliable sentence boundaries; spike M0 measured
adjacent-speech gaps at 0.02-0.1 s vs clear sentence pauses).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..transcribe.parser import Word
from .candidates import REASON_PRIORITY, punct_level

_LATIN_RE = re.compile(r'[A-Za-z0-9]')


def is_cjk(text: str) -> bool:
    """True when the text contains any CJK character (port of trans-jimaku)."""
    for ch in text:
        if ('一' <= ch <= '鿿' or
                '぀' <= ch <= 'ゟ' or
                '゠' <= ch <= 'ヿ' or
                '가' <= ch <= '힯'):
            return True
    return False


def _needs_space(prev_ch: str, next_ch: str) -> bool:
    """Space only between two latin/alnum characters (CJK glues directly)."""
    return bool(_LATIN_RE.match(prev_ch) and _LATIN_RE.match(next_ch))


def join_words(words: list[Word]) -> str:
    """Rebuild display text from word tokens.

    MAI emits CJK text as one char per word (punctuation included) and
    latin words as whole tokens, so CJK pieces glue with no space and
    latin-latin pairs get one space: ``Hello，`` + ``大`` -> ``Hello，大``.
    """
    out = ""
    for w in words:
        t = w.text
        if not t:
            continue
        if not out:
            out = t
        elif _needs_space(out[-1], t[0]):
            out += " " + t
        else:
            out += t
    return out


@dataclass
class SegmentParams:
    max_duration_s: float = 12.0
    max_chars: int = 60
    min_duration_s: float = 1.2   # set 0 to disable
    min_chars: int = 5            # 0 disables the char test
    split_pause_s: float = 0.8


def piece_duration(words: list[Word]) -> float:
    return words[-1].end - words[0].start if words else 0.0


def piece_chars(words: list[Word]) -> int:
    return len(join_words(words))


def is_over_limit(words: list[Word], p: SegmentParams) -> bool:
    return piece_duration(words) > p.max_duration_s or piece_chars(words) > p.max_chars


def is_too_short(words: list[Word], p: SegmentParams) -> bool:
    if not words:
        return False
    if piece_duration(words) < p.min_duration_s:
        return True
    return p.min_chars > 0 and piece_chars(words) < p.min_chars


def merge_short(pieces: list[list[Word]], p: SegmentParams) -> list[list[Word]]:
    """PLAN Pass A / Pass C: merge short pieces into an adjacent piece.

    Used on post-split fragments inside one run (same speaker by
    construction -- never merges across speakers). Each short piece tries
    the neighbour with the smaller inter-piece word gap first (ties prefer
    the next piece); a merge is accepted only if the result stays within
    BOTH max limits, otherwise the other side is tried; if neither fits
    the piece is left as-is (final formatting props up its display time).
    Loops until no merge succeeds -- every merge reduces the piece count,
    so this terminates.
    """
    pieces = [x[:] for x in pieces]
    while len(pieces) > 1:
        merged_idx = None
        for i, piece in enumerate(pieces):
            if not is_too_short(piece, p):
                continue
            options = []
            if i > 0:
                gap = piece[0].start - pieces[i - 1][-1].end
                options.append((gap, 1, i - 1))          # prev: tie loses
            if i < len(pieces) - 1:
                gap = pieces[i + 1][0].start - piece[-1].end
                options.append((gap, 0, i + 1))          # next: tie wins
            options.sort(key=lambda t: (t[0], t[1]))
            for _, _, j in options:
                merged = pieces[j] + piece if j < i else piece + pieces[j]
                if not is_over_limit(merged, p):
                    lo, hi = min(i, j), max(i, j)
                    pieces[lo:hi + 1] = [merged]
                    merged_idx = i
                    break
            if merged_idx is not None:
                break
        if merged_idx is None:
            break
    return pieces


_MAX_DEPTH = 10
_MIN_WORDS = 3


def split_fallback(
    words: list[Word], p: SegmentParams, depth: int = 0
) -> list[list[Word]]:
    """Deterministic recursive split for over-limit runs (LLM unavailable).

    Cut index selection per half: strongest reason (final > semicolon >
    ellipsis > comma > pause), then closest to the word-count centre;
    bare midpoint as the last resort. Returns the run unsplit when depth
    exceeds the cap or the word count is trivially small.
    """
    if depth > _MAX_DEPTH or len(words) <= _MIN_WORDS:
        return [words]
    if not is_over_limit(words, p):
        return [words]

    center = (len(words) - 1) / 2
    best_idx = -1
    best_key: tuple[int, float] | None = None
    for i in range(len(words) - 1):
        level = punct_level(words[i].text)
        if level is not None:
            prio = REASON_PRIORITY[level]
        elif words[i + 1].start - words[i].end >= p.split_pause_s:
            prio = REASON_PRIORITY["pause"]
        else:
            continue
        key = (prio, abs(i - center))
        if best_key is None or key < best_key:
            best_key = key
            best_idx = i

    if best_idx < 0:
        best_idx = int(center)   # no signal at all: midpoint hard cut

    return (
        split_fallback(words[:best_idx + 1], p, depth + 1)
        + split_fallback(words[best_idx + 1:], p, depth + 1)
    )
