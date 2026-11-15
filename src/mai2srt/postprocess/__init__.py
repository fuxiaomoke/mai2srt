"""Post-processing layer (PLAN 5 + the 2026-11-15 fuses).

Two deterministic passes run BEFORE the PLAN 5 phases, because they fix what
the segmenter cannot know: a line may not straddle a >= 3 s silence
(``pipeline._silence_split``) and a one-character line is an artifact that
folds into a neighbour (``pipeline._fold_tiny``). Then the phases proper:

* dialogue clustering -- MAI's X-Y-X "speaker return" (``detect_dialogue``);
* smart merge, then the over-length safety split;
* timestamp shaping -- outward expansion, then ``final_format``, which never
  moves a line's start: the word stream is the truth about when it begins.
"""
from __future__ import annotations

from .dialogue import SubEntry, detect_dialogue
from .merge import can_merge, merge_benefit, smart_merge
from .pipeline import PostprocessParams, process
from .timeline import expand_outward, final_format

__all__ = [
    "PostprocessParams",
    "SubEntry",
    "can_merge",
    "detect_dialogue",
    "expand_outward",
    "final_format",
    "merge_benefit",
    "process",
    "smart_merge",
]
