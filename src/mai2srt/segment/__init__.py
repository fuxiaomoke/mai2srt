"""Segmentation layer (PLAN 4): trust MAI timestamps, LLM only refines cuts."""
from __future__ import annotations

from .candidates import Candidate, find_candidates
from .llm import LLMSplitError, OpenAICompatibleSplitter, SplitTask, Splitter
from .pipeline import Segment, segment_transcript
from .rules import (
    SegmentParams,
    is_over_limit,
    is_too_short,
    join_words,
    merge_short,
    split_fallback,
)
from .turns import Turn, build_turns

__all__ = [
    "Candidate",
    "LLMSplitError",
    "OpenAICompatibleSplitter",
    "Segment",
    "SegmentParams",
    "SplitTask",
    "Splitter",
    "Turn",
    "build_turns",
    "find_candidates",
    "is_over_limit",
    "is_too_short",
    "join_words",
    "merge_short",
    "segment_transcript",
    "split_fallback",
]
