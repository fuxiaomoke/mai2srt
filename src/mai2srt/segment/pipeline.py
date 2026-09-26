"""Segmentation pipeline (PLAN 4): turns -> three-way judgement -> Pass B/C.

Per turn (speaker-continuous run, see turns.py):
  * within limits          -> kept as-is (origin "run"); run-level Pass A is
                              implicit -- maximal runs already glued same-
                              speaker neighbours, and merging across turns
                              is forbidden (speaker structure survives for
                              dialogue detection);
  * over limits            -> Pass B: candidate cut points, LLM picks the
                              semantic ones, each resulting piece still over
                              a limit gets a deterministic top-up split;
  * post-split fragments   -> Pass C: merge_short rescues sub-minimum
                              fragments among same-run siblings;
  * sentence endings       -> Pass D (_complete_sentences): mechanically
                              enforce "a line ends at sentence-final
                              punctuation unless the limits force otherwise"
                              -- the prompt is advisory, the pass is not.

LLM involvement is fully optional: splitter=None or any backend failure
degrades every affected run to the punctuation-recursive fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..transcribe.parser import Transcript, Word
from .candidates import find_candidates
from .llm import SplitTask, Splitter
from .rules import (
    SegmentParams,
    is_over_limit,
    is_too_short,
    join_words,
    merge_short,
    split_fallback,
)
from .turns import Turn, build_turns

Log = Callable[[str], None]


@dataclass
class Segment:
    """One final subtitle line candidate; timestamps are zero-loss word refs."""

    speaker: str | None
    words: list[Word] = field(default_factory=list)
    origin: str = "run"   # run | llm | fallback (merge keeps the split origin)

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return join_words(self.words)

    @property
    def duration(self) -> float:
        return self.end - self.start


def segment_transcript(
    t: Transcript,
    params: SegmentParams | None = None,
    splitter: Splitter | None = None,
    log: Log | None = None,
    llm_status: list[str] | None = None,
) -> list[Segment]:
    """Turn a stitched transcript into final subtitle segments.

    ``llm_status`` (optional out-param): the splitter failure message is
    appended when the LLM call fails and the fallback took over, so callers
    can surface a warning instead of silently degrading.
    """
    p = params or SegmentParams()
    emit: Log = log or (lambda m: None)
    words = sorted(t.words, key=lambda w: w.start)
    turns = build_turns(words)

    final: list[Segment] = []
    tasks: list[SplitTask] = []

    for turn in turns:
        if is_over_limit(turn.words, p):
            tasks.append(_prepare_task(len(tasks), turn, p, emit))
        else:
            final.append(Segment(turn.speaker, turn.words, "run"))

# Pass B (batched LLM choice, per-run deterministic safety net)
    chosen: dict[int, list[list[Word]]] = {}
    if tasks and splitter is not None:
        try:
            chosen = splitter.choose(tasks, p)
        except Exception as e:  # noqa: BLE001 -- any backend failure degrades
            emit("LLM splitter failed (%s); all %d over-limit runs use fallback"
                 % (e, len(tasks)))
            if llm_status is not None:
                llm_status.append(str(e))
            chosen = {}

    for task in tasks:
        picked = chosen.get(task.run_id)
        if picked:
            # pieces arrive as exact word spans (aligned free-text lines);
            # anything still over a limit gets the deterministic top-up
            pieces: list[list[Word]] = []
            for pc in picked:
                if is_over_limit(pc, p):
                    pieces.extend(split_fallback(pc, p))
                else:
                    pieces.append(pc)
        else:
            pieces = split_fallback(task.words, p)
        pieces = merge_short(pieces, p)          # Pass C (sub-min rescue)
        # Pass D (_complete_sentences) is HELD INACTIVE (M6f user decision):
        # punctuation is not a trustworthy semantic signal across languages,
        # and a transcription comma may legitimately BE a sentence end.
        # Kept in this module as a future per-language fallback.
        origin = "llm" if picked else "fallback"
        speaker = task.words[0].speaker
        final.extend(Segment(speaker, pc, origin) for pc in pieces)

    final.sort(key=lambda s: s.start)
    return final


def _prepare_task(run_id: int, turn: Turn, p: SegmentParams, emit: Log) -> SplitTask:
    # candidates are no longer sent to the LLM (M6f free-text task shape);
    # they stay on SplitTask for fallback diagnostics / future use
    return SplitTask(run_id, turn.words, find_candidates(turn.words, p.split_pause_s))


def _complete_sentences(pieces: list[list[Word]], p: SegmentParams) -> list[list[Word]]:
    """[INACTIVE -- M6e, held per M6f user decision] Mechanical enforcement
    of the sentence-final-ending rule. See the call-site comment in
    segment_transcript; unit-tested in test_m6e_completion.py and kept as a
    candidate per-language fallback.

    The prompt is advisory -- measured on real dual-speaker audio the model
    picks rhythmically spaced comma cuts and skips sentence-final candidates.
    This pass guarantees the invariant instead: a piece ends at a non-final
    boundary ONLY when the limits force it.

      (a) internal-final split -- a dangling piece that CONTAINS a sentence-
          final boundary splits there when the left half stays within limits
          (recovers final cuts the model skipped);
      (b) forward completion -- a still-dangling piece merges into its
          successor while the result stays within BOTH limits.
    """
    from .candidates import ends_sentence_final, punct_level

    # (a) internal-final split: recover sentence-final cuts the model skipped.
    # Guards: the LEFT half must be viable on its own (a sub-min left would
    # regress Pass C's tiny-sentence rescue into an orphan line); a sub-min
    # RIGHT half is fine whenever a successor piece exists -- step (b)
    # completes it forward immediately (measured case: 至近距離で、 ~1.0s
    # alone, 6.9s once merged with the sentence that follows it).
    expanded: list[list[Word]] = []
    for idx, pc in enumerate(pieces):
        split_at = None
        if not ends_sentence_final(join_words(pc)):
            # last internal final boundary first -> the dangling tail stays
            # as short as possible for the forward merge
            for k in range(len(pc) - 2, -1, -1):
                if punct_level(pc[k].text) in ("punct_final", "punct_ellipsis"):
                    left, right = pc[:k + 1], pc[k + 1:]
                    if (not is_over_limit(left, p)
                            and not is_too_short(left, p)
                            and (not is_too_short(right, p)
                                 or idx < len(pieces) - 1)):
                        split_at = k
                        break
        if split_at is not None:
            expanded.append(pc[:split_at + 1])
            expanded.append(pc[split_at + 1:])
        else:
            expanded.append(pc)

    # (b) forward completion: a dangling piece absorbs its successor while
    # the result stays within BOTH limits
    out: list[list[Word]] = []
    for pc in expanded:
        if (out and not ends_sentence_final(join_words(out[-1]))
                and not is_over_limit(out[-1] + pc, p)):
            out[-1] = out[-1] + pc
        else:
            out.append(pc)
    return out
