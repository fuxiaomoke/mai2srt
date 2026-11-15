"""Line-level dialogue detection: the MAI word-order interleave (2026-11-15).

Design (ported from trans-jimaku-web's ``_detect_mai_dialogue``, which deleted
the algorithm this module used to implement):

MAI encodes simultaneous speech as WORD-ORDER INTERLEAVING. Word times stay
strictly sequential (zero overlap, measured), but an interrupted speaker
RETURNS to the word stream after the interruption, so the two speakers'
sentence ranges end up overlapping each other and the line-level speaker
sequence reads X Y X (X Y Z X with three voices). That return is the documented
signal, and it is much stronger than a single label flip.

What this module deliberately does NOT do any more: union two adjacent lines
just because their speaker labels differ and their gap is small. That rule was
measured against ground truth on a 55-min three-voice source and every one of
the 31 "dialogue" lines it produced was a single sentence split in half -- the
worst passage is a deliberate A/B duet read of a title (A: 爆乳彼女と /
B: 隣の爆乳お姉さんの / together: 彼氏をかけた奪い合いレズバトル) where the
diarizer puts the handoffs one word off, so the old rule produced one subtitle
line per word and then regrouped them by speaker (scrambling the text order:
「奪い合いレズバトル」 -> "- 奪いレズバル / - 合いト").

Consequences of the new rule:

* a line must be SINGLE-SPEAKER to take part (a line whose words carry two
  labels is a diarization accident, not a voice: it never seeds and never
  joins);
* a region grows only while the next line is ADJACENT (start <= previous end
  + tolerance) and either repeats a speaker already in the region (the X Y X
  return) or introduces a new one while under the speaker cap;
* grouping text per speaker -- the thing that scrambled the old output -- is
  CORRECT for a genuine interleave (each speaker's words stay in order), so it
  is kept;
* regions stay inside a hard 16 s / 100 char cap, and a region that would
  exceed either is left as separate lines.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..segment.rules import is_cjk as _is_cjk


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


def _glue_seq(parts: list[str]) -> str:
    out = ""
    for t in parts:
        out = t if not out else _glue(out, t)
    return out


def single_speaker(e: SubEntry) -> str | None:
    """The line's one speaker, or None when unknown OR mixed.

    A line holding words from two labels is not "speaker A" for dialogue
    purposes: pretending it is is exactly what produced the old fake pairings.
    """
    labels = {s for s in e.speakers if s is not None}
    return labels.pop() if len(labels) == 1 else None


def detect_dialogue(
    entries: list[SubEntry],
    tolerance_s: float = 0.2,
    max_speakers: int = 3,
    max_duration_s: float = 16.0,
    max_chars: int = 100,
    log=None,
) -> list[SubEntry]:
    """Cluster X-Y-X interleave regions into `- A\\n- B` dialogue lines.

    Returns a new list; solo entries pass through untouched (never rewritten,
    never split). Lines with an unknown or mixed speaker never join a region.
    """
    emit = log or (lambda m: None)
    n = len(entries)
    if n < 2:
        return list(entries)

    sp = [single_speaker(e) for e in entries]

    # --- seed scan: X Y X (or X Y Z X), adjacency required ------------------
    regions: list[tuple[int, int]] = []
    i = 2
    free_from = 0
    while i < n:
        if i - 2 < free_from:
            i += 1
            continue
        a, b, c = sp[i - 2], sp[i - 1], sp[i]
        seed = None
        if (a is not None and b is not None and c is not None
                and c != b
                and entries[i].start <= entries[i - 1].end + tolerance_s):
            if c == a:
                seed = i - 2
            elif i >= 3 and sp[i - 3] is not None and c == sp[i - 3]:
                seed = i - 3            # X Y Z X: the return is one line later
        if seed is None:
            i += 1
            continue
        if len({sp[k] for k in range(seed, i + 1) if sp[k]}) > max_speakers:
            i += 1                      # X Y Z X needs the three-voice cap
            continue
        # EVERY pair inside the seed window must be adjacent, not just the last
        # one: a 3 s hole between the first two lines means two separate
        # exchanges, and merging them made a "dialogue" line that hung over
        # dead air (measured: a 3.2 s hole inside one merged line).
        if any(entries[k + 1].start > entries[k].end + tolerance_s
               for k in range(seed, i)):
            i += 1
            continue

        # --- grow while adjacent -------------------------------------------
        last = i
        members = {sp[k] for k in range(seed, i + 1) if sp[k]}
        while last + 1 < n:
            nxt = sp[last + 1]
            if nxt is None:
                break
            if entries[last + 1].start > entries[last].end + tolerance_s:
                break
            if nxt in members:
                last += 1
            elif len(members) < max_speakers:
                members.add(nxt)
                last += 1
            else:
                break
        regions.append((seed, last))
        free_from = last + 1
        i = last + 1

    if not regions:
        return list(entries)

    # --- build the dialogue lines ------------------------------------------
    out: list[SubEntry] = []
    pos = 0
    for first, last in regions:
        out.extend(entries[pos:first])
        members = entries[first:last + 1]
        order: list = []            # first-appearance speaker order
        frags: dict = {}            # speaker -> list[str]
        region_words: list = []
        speakers: set = set()
        for m in members:
            who = single_speaker(m) or "?"
            speakers.update(m.speakers)
            if who not in frags:
                frags[who] = []
                order.append(who)
            frags[who].append(m.text)
            region_words.extend(m.words)
        start = min(m.start for m in members)
        end = max(m.end for m in members)
        text = "\n".join("- " + _glue_seq(frags[k]) for k in order if frags[k])
        if len(text) > max_chars:
            emit("dialogue merge skipped: %d chars over the %d-char cap"
                 % (len(text), max_chars))
            out.extend(members)
            pos = last + 1
            continue
        if end - start > max_duration_s:
            emit("dialogue merge skipped: %.1fs over the %.1fs cap"
                 % (end - start, max_duration_s))
            out.extend(members)
            pos = last + 1
            continue
        emit("dialogue merged: %d lines, %d speakers | %s"
             % (len(members), len(order), text.replace("\n", " / ")[:40]))
        out.append(SubEntry(
            start, end, text, speakers, is_dialogue=True, words=region_words,
        ))
        pos = last + 1
    out.extend(entries[pos:])
    out.sort(key=lambda e: e.start)
    return out
