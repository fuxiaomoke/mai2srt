"""SRT serialization."""
from __future__ import annotations

from .postprocess.dialogue import SubEntry


def format_ts(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def to_srt(entries: list[SubEntry], start_index: int = 1) -> str:
    blocks = []
    for i, e in enumerate(entries, start_index):
        blocks.append(
            "%d\n%s --> %s\n%s\n" % (i, format_ts(e.start), format_ts(e.end), e.text)
        )
    return "\n".join(blocks)
