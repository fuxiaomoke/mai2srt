"""Speaker-continuous runs (turns) rebuilt from the word stream.

MAI utterances ARE speaker-continuous runs, but their boundaries are unstable
across runs (spike M0: same audio -> 267 vs 14 utterances). Words and their
timestamps are stable, so the segmentation layer anchors everything to the
word stream: adjacent same-speaker words are grouped into one maximal run.
This implicitly performs PLAN 4.1 "Pass A" merge at the utterance level
(same-speaker neighbours are already glued), so Pass A proper only runs on
post-split fragments (PLAN Pass C).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..transcribe.parser import Word


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
