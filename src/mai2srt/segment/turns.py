"""Speaker-continuous runs (turns) rebuilt from the word stream.

MAI utterances ARE speaker-continuous runs, but their boundaries are unstable
across runs (spike M0: same audio -> 267 vs 14 utterances). Words and their
timestamps are stable, so the segmentation layer anchors everything to the
word stream: adjacent same-speaker words are grouped into one maximal run.
This implicitly performs PLAN 4.1 "Pass A" merge at the utterance level
(same-speaker neighbours are already glued), so Pass A proper only runs on
post-split fragments (PLAN Pass C).

FRAGMENT ABSORPTION (2026-11-15, A/B/合 audit). The run split above assumes a
speaker change is a real turn boundary. Measured on a 55-min three-voice
source it often is not: word-level diarization flips INSIDE a phrase, so
``build_turns`` produced 373 runs for 3328 s and 47 of them held a single
character (「の」「ト」「ル」). The ground truth for the worst passage is a
deliberate A/B duet read (A: 爆乳彼女と / B: 隣の爆乳お姉さんの / 合: 彼氏を
かけた奪い合いレズバトル) -- the alternation is real, but the labels put the
handoffs one word off, so trusting them produces one subtitle line per word.

``absorb_fragments`` adds the missing judgement in two conservative steps:

1. **micro fold** -- a run of ONE character cannot be read as a line at all, so
   it joins the neighbour it is closer to (1-char runs 47 -> 3).
2. **chain fold** -- five or more CONSECUTIVE sub-second runs (each under 1.5 s
   or 5 chars, each gap under 0.5 s) are not a conversation, they are the
   diarizer flipping inside a phrase; the chain collapses into as few lines as
   the duration/char caps allow. The worst passage (13 turns over 16 s, one
   character each) becomes three readable lines, and the text keeps its order.

Why the chain needs FIVE members: a genuine fast exchange looks the same at
four -- trans-jimaku-web's flagship user sample (「ほら、」/「や-」/
「ブラコンじゃん。」/「ブラコン。」, every turn 0.2-1.0 s) must keep its
dialogue line, and it does. Below the chain threshold nothing is folded, so a
real two-person bicker is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..transcribe.parser import Word
from .rules import join_words

#: a run below EITHER floor cannot stand as a subtitle line on its own.
#: Deliberately conservative (ONE character, no duration test): measured on the
#: 55-min source a wider floor (``< 1.5 s or < 5 chars``) folded 373 runs down to
#: 309 and killed the fragment lines, but it also destroyed genuine rapid
#: dialogue -- trans-jimaku-web's flagship sample (「ほら、」/「や-」/
#: 「ブラコンじゃん。」/「ブラコン。」, every turn 0.2-1.0 s) stopped being a
#: dialogue line at all. A run that is a whole character has no reading of its
#: own; anything longer gets to keep its own line.
ABSORB_MIN_DURATION_S = 0.0
ABSORB_MIN_CHARS = 2
#: only fold across a gap this short: past it the silence itself is the
#: boundary, and the fragment earns its own line (a real short reply)
ABSORB_MAX_GAP_S = 0.5

#: what "too short to be a turn" means for the CHAIN rule (step 2)
TINY_DURATION_S = 1.5
TINY_CHARS = 5
#: how many such runs in a row stop looking like a conversation. Four is a
#: real fast exchange (see the module docstring); five and up is the diarizer
#: flipping inside a phrase.
TINY_CHAIN_MIN = 5


@dataclass
class Turn:
    """One maximal speaker-continuous word run."""

    speaker: str | None
    words: list[Word] = field(default_factory=list)

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def build_turns(words: list[Word]) -> list[Turn]:
    """Group a time-sorted word stream into maximal same-speaker runs.

    ``None`` speakers group together (treated as one mono voice). Words must
    already be sorted by start (the caller sorts).
    """
    turns: list[Turn] = []
    for w in words:
        if turns and turns[-1].speaker == w.speaker:
            turns[-1].words.append(w)
        else:
            turns.append(Turn(w.speaker, [w]))
    return turns


def _speaker_of(words: list[Word]) -> str | None:
    """The run's speaker when every word agrees, else None (mixed = unknown)."""
    labels = {w.speaker for w in words if w.speaker is not None}
    return labels.pop() if len(labels) == 1 else None


def _tiny(words: list[Word]) -> bool:
    """Too short to be a turn: under either the duration or the char floor."""
    return ((words[-1].end - words[0].start) < TINY_DURATION_S
            or len(join_words(words)) < TINY_CHARS)


def _fold_micro(runs: list[list[Word]], max_duration_s: float,
                max_chars: int, min_duration_s: float, min_chars: int,
                max_gap_s: float) -> list[list[Word]]:
    """Step 1: fold runs below the (one-character) floor into a neighbour."""
    while len(runs) > 1:
        folded = False
        for i, words in enumerate(runs):
            if (words[-1].end - words[0].start) >= min_duration_s and \
                    len(join_words(words)) >= min_chars:
                continue
            options: list[tuple[float, int]] = []
            if i > 0:
                options.append((words[0].start - runs[i - 1][-1].end, i - 1))
            if i < len(runs) - 1:
                options.append((runs[i + 1][0].start - words[-1].end, i + 1))
            if not options:
                continue
            gap, j = min(options)
            if gap > max_gap_s:
                continue
            lo, hi = min(i, j), max(i, j)
            merged = runs[lo] + runs[hi]
            if (merged[-1].end - merged[0].start) > max_duration_s:
                continue
            if len(join_words(merged)) > max_chars:
                continue
            runs[lo:hi + 1] = [merged]
            folded = True
            break
        if not folded:
            break
    return runs


def _fold_chains(runs: list[list[Word]], max_duration_s: float,
                 max_chars: int, max_gap_s: float) -> list[list[Word]]:
    """Step 2: collapse runs of ``TINY_CHAIN_MIN``+ consecutive tiny runs.

    A chain that long is the diarizer flipping inside a phrase, not a
    conversation: the pieces are merged left to right, each merge still
    respecting the duration/char caps so a 16 s flapping stretch becomes the
    fewest legal lines instead of thirteen.
    """
    if len(runs) < TINY_CHAIN_MIN:
        return runs
    tiny = [_tiny(w) for w in runs]
    targets: set[int] = set()
    i = 0
    while i < len(runs):
        if not tiny[i]:
            i += 1
            continue
        j = i
        while (j + 1 < len(runs) and tiny[j + 1]
               and (runs[j + 1][0].start - runs[j][-1].end) < max_gap_s):
            j += 1
        if j - i + 1 >= TINY_CHAIN_MIN:
            targets.update(range(i, j + 1))
        i = j + 1
    if not targets:
        return runs
    out: list[list[Word]] = []
    for k, words in enumerate(runs):
        if k in targets and out and (words[0].start - out[-1][-1].end) < max_gap_s:
            merged = out[-1] + words
            if ((merged[-1].end - merged[0].start) <= max_duration_s
                    and len(join_words(merged)) <= max_chars):
                out[-1] = merged
                continue
        out.append(list(words))
    return out


def absorb_fragments(
    turns: list[Turn],
    max_duration_s: float = 12.0,
    max_chars: int = 60,
    min_duration_s: float = ABSORB_MIN_DURATION_S,
    min_chars: int = ABSORB_MIN_CHARS,
    max_gap_s: float = ABSORB_MAX_GAP_S,
) -> list[Turn]:
    """Fold fragment runs into their neighbours (2026-11-15, two steps).

    Step 1 (micro): a run below the one-character floor joins the neighbour
    with the smaller inter-run gap (ties prefer the NEXT run -- speech flows
    forward), as long as the fold stays inside the duration/char limits and
    does not bridge a gap wider than ``max_gap_s``.

    Step 2 (chain): a chain of ``TINY_CHAIN_MIN``+ consecutive tiny runs is
    collapsed the same way.

    Folding crosses speaker boundaries on purpose; the merged run then reports
    ``speaker=None`` because its words disagree, which is exactly what the
    dialogue pass needs to know. Speech that is genuinely short -- 「ね？」
    「あら？」 -- is untouched whenever a real pause (>= ``max_gap_s``) isolates
    it, and short exchanges of four turns or fewer are never folded.
    """
    runs: list[list[Word]] = [list(t.words) for t in turns if t.words]
    if not runs:
        return []
    runs = _fold_micro(runs, max_duration_s, max_chars,
                       min_duration_s, min_chars, max_gap_s)
    runs = _fold_chains(runs, max_duration_s, max_chars, max_gap_s)
    return [Turn(_speaker_of(w), w) for w in runs]

