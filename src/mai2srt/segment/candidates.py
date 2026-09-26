"""Candidate split points inside one over-limit run (PLAN 4.1 Pass B step 1).

A cut point always lands on a word boundary, so splitting is lossless for
timestamps. Two signals produce candidates:

* punctuation -- the word IS a standalone punctuation token (CJK shape:
  MAI emits ``、`` / ``。`` as separate words with their own timestamps) or
  ENDS with punctuation (latin shape: ``Hello，``);
* inter-word pause >= ``split_pause_s``.

Punctuation sets are ported verbatim from trans-jimaku-web postprocess.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..transcribe.parser import Word

FINAL_PUNCTUATION = {'。', '？', '?', '！', '!', '.'}
ELLIPSIS_PUNCTUATION = {'…', '‥', '...', '......'}
COMMA_PUNCTUATION = {'，', '、', ','}
SEMICOLON_PUNCTUATION = {'；', ';'}


@dataclass
class Candidate:
    index: int    # split AFTER words[index]
    reason: str   # punct_final | punct_semicolon | punct_ellipsis | punct_comma | pause


#: lower number = stronger signal. Punctuation (a semantic cue from the model)
#: outranks pause (an acoustic cue); among punctuation, sentence-final wins.
REASON_PRIORITY: dict[str, int] = {
    "punct_final": 0,
    "punct_semicolon": 1,
    "punct_ellipsis": 2,
    "punct_comma": 3,
    "pause": 4,
}

_PUNCT_LEVELS = (
    ("punct_final", FINAL_PUNCTUATION),
    ("punct_semicolon", SEMICOLON_PUNCTUATION),
    ("punct_ellipsis", ELLIPSIS_PUNCTUATION),
    ("punct_comma", COMMA_PUNCTUATION),
)

_ELLIPSIS_RE = re.compile(r'(?:…+|‥+|\.{3,})$')


def punct_level(word_text: str) -> str | None:
    """Strongest punctuation level of a word (is, or ends with, a token)."""
    cleaned = word_text.strip()
    if not cleaned:
        return None
    for name, pset in _PUNCT_LEVELS:
        if cleaned in pset or any(cleaned.endswith(p) for p in pset):
            return name
    if _ELLIPSIS_RE.search(cleaned):
        return "punct_ellipsis"
    return None


def find_candidates(words: list[Word], split_pause_s: float) -> list[Candidate]:
    """Deduped candidate cut points, sorted by word index.

    The last word never yields a candidate (cutting there is a no-op). When
    the same position qualifies as both punctuated and paused, the stronger
    reason is kept.
    """
    best: dict[int, Candidate] = {}
    for i in range(len(words) - 1):
        level = punct_level(words[i].text)
        if level is not None:
            best[i] = Candidate(i, level)      # punctuation always wins over pause
        elif words[i + 1].start - words[i].end >= split_pause_s:
            best[i] = Candidate(i, "pause")
    return [best[i] for i in sorted(best)]


def ends_sentence_final(text: str) -> bool:
    """True when the display text closes a sentence (final or trailing-
    ellipsis punctuation). The completion pass uses this to decide whether a
    subtitle line "dangles" mid-sentence."""
    t = (text or "").strip()
    if not t:
        return False
    if t[-1] in FINAL_PUNCTUATION or t[-1] in ELLIPSIS_PUNCTUATION:
        return True
    return bool(_ELLIPSIS_RE.search(t[-6:]))
