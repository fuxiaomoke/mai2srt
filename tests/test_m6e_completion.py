# -*- coding: utf-8 -*-
"""M6e sentence-completion tests: the mechanical pass that guarantees lines
end at sentence-final punctuation unless the limits force otherwise."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.postprocess.merge import MergeParams, can_merge
from mai2srt.postprocess.dialogue import SubEntry
from mai2srt.segment.candidates import ends_sentence_final
from mai2srt.segment.pipeline import _complete_sentences
from mai2srt.segment.rules import SegmentParams, join_words
from mai2srt.transcribe.parser import Word


def W(text, start, speaker="1"):
    return Word(text, start, start + 0.5, speaker)


# --------------------------------------------------------------- predicate

def test_ends_sentence_final():
    assert ends_sentence_final("マッサージ。")
    assert ends_sentence_final("ですか？")
    assert ends_sentence_final("done.")
    assert ends_sentence_final("そう...")
    assert ends_sentence_final("至近距離で…")
    assert not ends_sentence_final("至近距離で、")
    assert not ends_sentence_final("コースで")
    assert not ends_sentence_final("")


# ------------------------------------------------------- completion pass

def test_completion_recovers_skipped_final_cut():
    """The 双耳测试 case: LLM picked comma cuts [22c,][19c ending 、 with an
    internal 。][23c。] -> the pass must reproduce the ideal [36c。][29c。]."""
    full = ("エルフの柔らかい体をぬるぬる密着させながら、ディープリンパマッサージ。"
            "至近距離で、ゆるおほごえを聞いていただく、癒しの空間です。")
    words = [Word(ch, i * 0.25, i * 0.25 + 0.2, "1") for i, ch in enumerate(full)]
    p = SegmentParams()                      # 12s / 60 chars
    i1 = full.index("、") + 1                 # after ながら、   (LLM cut 1)
    i2 = full.index("。") + 1                 # after マッサージ。(skipped final!)
    i3 = full.index("、", i2) + 1             # after 至近距離で、(LLM cut 3)
    # emulate the model's picks [1, 3]
    pieces = [words[:i1], words[i1:i3], words[i3:]]
    assert not ends_sentence_final(join_words(pieces[1]))
    out = _complete_sentences(pieces, p)
    texts = [join_words(pc) for pc in out]
    assert texts == [full[:i2], full[i2:]]
    for t in texts:
        assert ends_sentence_final(t)


def test_completion_forward_merge_within_limits():
    words = [W("あああ", 0.0), W("、", 1.0), W("いいい", 2.0), W("。", 3.0)]
    out = _complete_sentences([words[:2], words[2:]], SegmentParams())
    assert len(out) == 1
    assert join_words(out[0]) == "あああ、いいい。"


def test_completion_keeps_forced_dangling():
    """Sentence spans longer than max_duration -> the comma ending is
    legitimate and must survive (completion cannot merge past the limit)."""
    # two chunks 8s apart: merged duration 8.0s+... build pieces whose union
    # exceeds max_duration_s
    a = [Word("あ", 0.0, 7.0), Word("、", 7.0, 7.2)]       # 7.2s piece
    b = [Word("い", 8.0, 15.5), Word("。", 15.5, 15.7)]    # 7.7s piece
    p = SegmentParams(max_duration_s=10.0)
    out = _complete_sentences([a, b], p)
    assert len(out) == 2          # merged would be 15.7s > 10 -> kept apart
    assert not ends_sentence_final(join_words(out[0]))     # forced dangling


def test_completion_splits_when_tail_is_submin_but_completed():
    """Measured 双耳测试 shape: the dangling tail alone is sub-minimum
    (~1.0s) but a successor piece exists -> the split must fire and (b)
    completes the tail forward (regression test for the over-strict guard)."""
    full = "エルフの体を密着させながら、マッサージ。至近距離で、癒しの空間です。"
    words = [Word(ch, 0.0, 0.2, "1") for ch in full]
    # linear timing, except the dangling tail 至近距離で、 gets a tight 1.0s
    # window (sub-minimum alone, fine once completed forward)
    for k, w in enumerate(words):
        w.start, w.end = k * 0.25, k * 0.25 + 0.2
    i_tail = full.index("至近距離で")
    i_rest = full.index("癒しの空間")
    for k in range(i_tail, i_rest):
        words[k].start = 10.0 + (k - i_tail) * 0.16
        words[k].end = words[k].start + 0.1
    for k in range(i_rest, len(words)):
        words[k].start = 11.0 + (k - i_rest) * 0.25
        words[k].end = words[k].start + 0.2

    p = SegmentParams()
    i_comma1 = full.index("、") + 1
    i_final = full.index("。") + 1
    i_comma2 = full.index("、", i_final) + 1
    # LLM picked [1,3]: pieces [ながら、][マッサージ。至近距離で、][癒し…。]
    pieces = [words[:i_comma1], words[i_comma1:i_comma2], words[i_comma2:]]
    out = _complete_sentences(pieces, p)
    texts = [join_words(pc) for pc in out]
    assert texts == [full[:i_final], full[i_final:]]
    assert all(ends_sentence_final(t) for t in texts)


def test_completion_untouches_final_ended_multisentence():
    """[はい。 + dangling] merged lines (multi-sentence, ends 、) with a
    SUB-MIN left half must not be re-split by the guard, and stay dangling
    when merging forward would exceed limits."""
    words = [W("はい", 0.0), W("。", 0.5),
             W("左右から密着し", 1.0), W("、", 8.0),
             W("両耳に吹きかける", 8.2), W("。", 16.0)]
    p = SegmentParams(max_duration_s=10.0)
    pieces = [words[:4], words[4:]]        # [はい。…し、 8s] [両耳に…。 8s]
    out = _complete_sentences(pieces, p)
    # not split (left half [はい。] would be sub-min), forward merge 16s > 10
    # -> the dangling ending is forced and kept exactly as Pass C left it
    assert len(out) == 2
    assert join_words(out[0]) == "はい。左右から密着し、"


# ------------------------------------------------------------ merge veto

def _entry(text, start, end, dlg=False):
    return SubEntry(start, end, text, {"1"}, is_dialogue=dlg)


def test_smart_merge_final_plus_dangling_allowed_again():
    """M6e veto REVERTED per M6f user decision: punctuation is not a
    trustworthy semantic signal across languages, so a final-ended line
    merging with a dangling next line is judged by the generic vetoes only."""
    p = MergeParams()
    e1 = _entry("マッサージ。", 0.0, 3.0)
    e2 = _entry("至近距離で、", 3.1, 4.0)
    assert can_merge(e1, e2, p) is True


def test_smart_merge_allows_dangling_plus_final():
    p = MergeParams()
    e1 = _entry("左右から密着し、", 0.0, 4.0)
    e2 = _entry("両耳に吹きかけて差し上げます。", 4.1, 9.0)
    # completes the sentence -> allowed (duration/chars/cps all fine)
    assert can_merge(e1, e2, p) is True
