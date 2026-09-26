"""Stitch per-chunk transcripts into one document with absolute timestamps."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .parser import Transcript

log = logging.getLogger("mai2srt.stitch")


@dataclass
class ChunkMeta:
    file: str
    offset_s: float
    duration_s: float
    utterances: int
    words: int


@dataclass
class StitchedTranscript:
    transcript: Transcript
    chunks: list[ChunkMeta] = field(default_factory=list)
    audio_duration_s: float = 0.0     # authoritative (ffprobe of the source)


def stitch(parts: list[tuple[Transcript, float, str]],
           audio_duration_s: float) -> StitchedTranscript:
    """``parts``: (chunk transcript, offset seconds, chunk file name)."""
    merged = Transcript()
    metas: list[ChunkMeta] = []
    for t, offset, name in parts:
        for u in t.utterances:
            u.start += offset
            u.end += offset
        for w in t.words:
            w.start += offset
            w.end += offset
        merged.words.extend(t.words)
        merged.utterances.extend(t.utterances)
        metas.append(ChunkMeta(
            file=name, offset_s=offset, duration_s=t.duration_s,
            utterances=len(t.utterances), words=len(t.words)))
        log.info("chunk %s: offset=%.1fs utts=%d words=%d",
                 name, offset, len(t.utterances), len(t.words))
    merged.duration_s = audio_duration_s
    # majority language across parts
    votes: list[str] = [p.language for p, _, _ in parts if p.language]
    merged.language = max(set(votes), key=votes.count) if votes else None
    return StitchedTranscript(transcript=merged, chunks=metas,
                              audio_duration_s=audio_duration_s)
