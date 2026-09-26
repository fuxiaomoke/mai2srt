"""M2 segmentation layer unit tests (pure functions, no network)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.segment.candidates import find_candidates, punct_level
from mai2srt.segment.llm import SplitTask, _extract_json_object
from mai2srt.segment.pipeline import segment_transcript
from mai2srt.segment.rules import (
    SegmentParams,
    is_over_limit,
    is_too_short,
    join_words,
    merge_short,
    split_fallback,
)
from mai2srt.segment.turns import build_turns
from mai2srt.transcribe.parser import Transcript, Word


def W(text, start, end, speaker=None):
    return Word(text, start, end, speaker)


# ---------------------------------------------------------------- text join

def test_join_words_cjk_glues_latin_spaces():
    ja = [W("あ", 0, 0.1), W("の", 0.1, 0.2), W("、", 0.2, 0.3), W("す", 0.3, 0.4)]
    assert join_words(ja) == "あの、す"
    # latin words space-separate; a standalone "!" token glues back to the word
    en = [W("Hello", 0, 0.3), W("world", 0.4, 0.7), W("!", 0.7, 0.8)]
    assert join_words(en) == "Hello world!"
    mix = [W("Hello，", 0, 0.3), W("大", 0.3, 0.4), W("家", 0.4, 0.5), W("好", 0.5, 0.6)]
    assert join_words(mix) == "Hello，大家好"
    assert join_words([]) == ""


# ---------------------------------------------------------------- turns

def test_build_turns_groups_and_alternates():
    words = [
        W("a", 0, 1, "1"), W("b", 1, 2, "1"),
        W("c", 2, 3, "2"),
        W("d", 3, 4, "1"),
    ]
    turns = build_turns(words)
    assert [(t.speaker, len(t.words)) for t in turns] == [("1", 2), ("2", 1), ("1", 1)]
    assert turns[0].start == 0.0 and turns[0].end == 2.0

    mono = [W("a", 0, 1, None), W("b", 1, 2, None)]
    assert len(build_turns(mono)) == 1


# ---------------------------------------------------------------- candidates

def test_punct_level_shapes():
    assert punct_level("。") == "punct_final"
    assert punct_level("Hello，") == "punct_comma"
    # "そう..." ends with '.', so FINAL wins first -- identical to trans-jimaku-web;
    # "‥" (not dot-ending) reaches the ellipsis set
    assert punct_level("そう...") == "punct_final"
    assert punct_level("そう‥") == "punct_ellipsis"
    assert punct_level("…") == "punct_ellipsis"
    assert punct_level("すみません") is None
    assert punct_level("3.5") is None


def test_find_candidates_marks_and_pause():
    # standalone CJK punctuation words + a >=0.8s pause + no trailing cut
    words = [
        W("す", 0.0, 0.1), W("み", 0.1, 0.2), W("ま", 0.2, 0.3), W("せ", 0.3, 0.4),
        W("ん", 0.4, 0.5), W("。", 0.5, 0.6),
        W("は", 2.0, 2.1), W("い", 2.1, 2.2),   # 1.4s gap after 。 word
    ]
    cands = find_candidates(words, split_pause_s=0.8)
    # index 5 (。) is punct_final; index 5 also has the pause -- punct wins; no cand after last word
    assert [(c.index, c.reason) for c in cands] == [(5, "punct_final")]

    # no punctuation at all -> pause only
    words2 = [W("a", 0.0, 0.5), W("b", 1.6, 2.0)]
    c2 = find_candidates(words2, split_pause_s=0.8)
    assert [(c.index, c.reason) for c in c2] == [(0, "pause")]

    # pause below threshold -> nothing
    c3 = find_candidates(words2, split_pause_s=2.0)
    assert c3 == []


# ---------------------------------------------------------------- merge_short

def test_merge_short_prefers_smaller_gap_side():
    # piece P0 - short P1 - P2; P1 sits closer to P2 (smaller gap to next).
    # P0/P2 duration >= min so only P1 is short.
    p = SegmentParams(max_duration_s=30, max_chars=200, min_duration_s=1.2, min_chars=5)
    p0 = [W("aaaaaaaa", 0.0, 2.0)]
    p1 = [W("bb", 5.0, 5.5)]          # short (chars)
    p2 = [W("cccccccc", 6.0, 8.0)]
    out = merge_short([p0, p1, p2], p)
    assert len(out) == 2
    assert [w.text for w in out[1]] == ["bb", "cccccccc"]


def test_merge_short_tie_prefers_next():
    p = SegmentParams(max_duration_s=30, max_chars=200, min_duration_s=1.2, min_chars=5)
    p0 = [W("aaaaaaaa", 0.0, 2.0)]
    p1 = [W("bb", 3.0, 3.5)]          # gaps: 1.0 both sides
    p2 = [W("cccccccc", 4.5, 6.5)]
    out = merge_short([p0, p1, p2], p)
    assert len(out) == 2
    assert [w.text for w in out[1]] == ["bb", "cccccccc"]


def test_merge_short_respects_max_limits():
    # CJK pieces glue without space: 5+1=6 chars fits under max_chars=6 on
    # the gap-smaller side (prev gap 0.0 < next gap 0.5) -> merges forward
    p = SegmentParams(max_duration_s=30, max_chars=6, min_duration_s=1.2, min_chars=5)
    p0 = [W("あああああ", 0.0, 2.0)]   # 5 chars, not short itself
    p1 = [W("い", 2.0, 2.5)]           # short
    p2 = [W("ううううう", 3.0, 5.0)]
    out = merge_short([p0, p1, p2], p)
    assert len(out) == 2
    assert [w.text for w in out[0]] == ["あああああ", "い"]

    # tighter cap: 6 > 5 on both sides -> the short piece stays alone
    p_tight = SegmentParams(max_duration_s=30, max_chars=5, min_duration_s=1.2, min_chars=5)
    out2 = merge_short([p0, p1, p2], p_tight)
    assert len(out2) == 3


def test_merge_short_chain_until_stable():
    # CJK one-char words: every piece short -> collapses to a single piece
    p = SegmentParams(max_duration_s=100, max_chars=500, min_duration_s=1.2, min_chars=5)
    pieces = [
        [W("あ", 0.0, 0.5)], [W("い", 1.0, 1.5)], [W("う", 2.0, 2.5)], [W("え", 3.0, 3.5)],
    ]
    out = merge_short(pieces, p)
    assert len(out) == 1
    assert join_words(out[0]) == "あいうえ"


# ---------------------------------------------------------------- split_fallback

def test_split_fallback_punctuation_centre_recursive():
    # single cut: 30 one-char words, mid punctuation at 14 (。), halves fit
    p = SegmentParams(max_duration_s=12, max_chars=20)
    words = [W("あ", i * 0.2, i * 0.2 + 0.1) for i in range(30)]
    words[14] = W("。", 14 * 0.2, 14 * 0.2 + 0.1)
    out = split_fallback(words, p)
    assert len(out) == 2
    assert out[0][-1].text == "。" and len(out[0]) == 15
    for pc in out:
        assert not is_over_limit(pc, p)

    # tighter cap forces recursion on both halves; still lossless overall
    p2 = SegmentParams(max_duration_s=12, max_chars=8)
    out2 = split_fallback(words, p2)
    assert len(out2) > 2
    assert all(not is_over_limit(pc, p2) for pc in out2)
    flat = [w for pc in out2 for w in pc]
    assert flat == words


def test_split_fallback_pause_when_no_punct():
    p = SegmentParams(max_duration_s=12, max_chars=22)
    words = [W("あ", i * 0.2, i * 0.2 + 0.1) for i in range(30)]
    # 5s hole after word 20: shift the whole tail past it (keep time order)
    for j in range(21, 30):
        words[j] = W("あ", 9.0 + (j - 21) * 0.2, 9.0 + (j - 21) * 0.2 + 0.1)
    out = split_fallback(words, p)
    # the pause beats the midpoint: cut lands after word 20, not word 15
    assert len(out) == 2
    assert len(out[0]) == 21 and out[0][-1].end == words[20].end


def test_split_fallback_midpoint_when_no_signal():
    p = SegmentParams(max_duration_s=12, max_chars=10)
    words = [W("あ", i * 0.2, i * 0.2 + 0.1) for i in range(30)]
    out = split_fallback(words, p)
    assert len(out) >= 2
    assert all(not is_over_limit(pc, p) for pc in out)
    # reassembly is lossless
    flat = [w for pc in out for w in pc]
    assert flat == words


def test_split_fallback_keeps_small_runs():
    p = SegmentParams()
    words = [W("あ", 0, 0.5), W("い", 0.5, 1.0)]
    assert split_fallback(words, p) == [words]


# ---------------------------------------------------------------- pipeline

def _transcript_with_long_run():
    # one 20-word over-limit run (chars) with 。 after word 9, plus a short
    # different-speaker run so speaker structure survives
    words = [W("あ", i * 0.3, i * 0.3 + 0.2, "1") for i in range(20)]
    words[9] = W("。", 9 * 0.3, 9 * 0.3 + 0.2, "1")
    words.append(W("うん", 7.0, 7.2, "2"))
    return Transcript(words=words, duration_s=8.0, language="ja")


def test_segment_transcript_no_llm_end_to_end():
    t = _transcript_with_long_run()
    p = SegmentParams(max_duration_s=12, max_chars=8, min_duration_s=1.2, min_chars=3)
    segs = segment_transcript(t, p, splitter=None)
    assert all(not is_over_limit(s.words, p) for s in segs if len(s.words) > 3)
    # timestamps are the original word objects (zero loss)
    assert segs[0].words[0] in t.words
    # different-speaker run survives as its own segment
    assert any(s.speaker == "2" for s in segs)
    # overall order
    starts = [s.start for s in segs]
    assert starts == sorted(starts)


def test_segment_transcript_short_single_run_kept():
    t = Transcript(words=[W("はい", 0.0, 0.4, "1")], duration_s=0.5, language="ja")
    segs = segment_transcript(t, SegmentParams())
    assert len(segs) == 1
    assert segs[0].origin == "run" and is_too_short(segs[0].words, SegmentParams())


# ---------------------------------------------------------------- llm parsing

def test_extract_json_object_tolerant():
    assert _extract_json_object('{"results": [{"id": 1, "splits": [2]}]}')["results"][0]["splits"] == [2]
    assert _extract_json_object('好的，如下：\n```json\n{"results": []}\n```')['results'] == []
    assert _extract_json_object('前置噪音 {"results": []} 后置噪音')['results'] == []
    try:
        _extract_json_object("完全没有 JSON")
        raise AssertionError("should have raised")
    except Exception as e:
        assert "no JSON object" in str(e)


def test_split_task_marked_text():
    words = [W("あ", 0.0, 0.2), W("い", 0.2, 0.4), W("。", 0.4, 0.5), W("う", 0.5, 0.7), W("え", 0.7, 0.9)]
    cands = find_candidates(words, split_pause_s=0.8)
    task = SplitTask(0, words, cands)
    # only index 2 (。) is a candidate -> numbered 1
    assert task.marked_text() == "あい。[1|うえ"


def test_apply_choice_via_fake_splitter():
    from mai2srt.segment.llm import OpenAICompatibleSplitter  # noqa: F401 (import check)
    # fake splitter (M6f contract): return pieces = one per final-punct
    # sentence, exactly what a well-behaved LLM would align to
    from mai2srt.segment.candidates import punct_level

    class FakeSplitter:
        def choose(self, tasks, params):
            out = {}
            for t in tasks:
                pieces, cur = [], []
                for w in t.words:
                    cur.append(w)
                    if punct_level(w.text) == "punct_final":
                        pieces.append(cur)
                        cur = []
                if cur:
                    pieces.append(cur)
                if len(pieces) > 1:
                    out[t.run_id] = pieces
            return out

    t = _transcript_with_long_run()
    p = SegmentParams(max_duration_s=12, max_chars=8, min_duration_s=1.2, min_chars=3)
    segs = segment_transcript(t, p, splitter=FakeSplitter())
    assert any(s.origin == "llm" for s in segs)
    assert all(not is_over_limit(s.words, p) for s in segs if len(s.words) > 3)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("[ok] %s" % fn.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print("[FAIL] %s: %s" % (fn.__name__, e))
            traceback.print_exc()
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
