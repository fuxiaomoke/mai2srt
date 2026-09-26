"""Post-processing layer (PLAN 5): dialogue clustering + MAI timestamp profile."""
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
