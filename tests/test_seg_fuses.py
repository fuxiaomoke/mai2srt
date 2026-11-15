"""Segmentation fuses (2026-11-15): the deterministic guarantees that do not
depend on the model, and the pause information the model now receives.

Background, measured on a 55-min three-voice source:
  * word-level diarization flips inside a phrase, so "speaker changed" is not a
    turn boundary: 47 of 373 runs held a single character (「の」「ト」「ル」);
  * 45 of 436 subtitle lines straddled a >= 3 s silence (the line lingers over
    dead air, and the viewer reads two utterances as one);
  * propping a short line up to the 1.2 s minimum pushed every following line
    later (+4.9 s by 68 s).

The rules that answer those, and the boundaries between them, are pinned here:
a short exchange of four turns is a conversation and must survive; a chain of
five is the diarizer flipping and must collapse.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.postprocess.dialogue import SubEntry, detect_dialogue
from mai2srt.postprocess.pipeline import PostprocessParams, _fold_tiny, _silence_split
from mai2srt.segment.pipeline import segment_transcript
from mai2srt.segment.rules import SegmentParams
from mai2srt.segment.turns import TINY_CHAIN_MIN, build_turns, absorb_fragments
from mai2srt.postprocess.pipeline import process
from mai2srt.transcribe.parser import Transcript, Word


def W(text, start, end, speaker="1"):
    return Word(text, start, end, speaker)


def alternating(n: int, *, chars: int = 1, step: float = 0.2):
    """n adjacent turns, alternating speaker 1/2, one word each."""
    out = []
    t = 0.0
    for i in range(n):
        out.append(W("あ" * chars, t, t + step * 0.8, "1" if i % 2 == 0 else "2"))
        t += step
    return out


# ------------------------------------------------------- fragment absorption

def test_a_single_character_run_is_absorbed():
    words = [W("ああ", 0.0, 0.5), W("い", 0.6, 0.7, "2"), W("うう", 0.8, 1.3)]
    runs = absorb_fragments(build_turns(words))
    assert [w.text for run in runs for w in run.words] == ["ああ", "い", "うう"]
    assert all(len("".join(w.text for w in run.words)) > 1 for run in runs)


def test_four_turn_burst_survives_but_five_collapses():
    """trans-jimaku-web's flagship sample is a FOUR-turn burst (「ほら、」「や-」
    「ブラコンじゃん。」「ブラコン。」): folding it would delete a real dialogue
    line. Five or more is the label flipping inside a phrase."""
    four = absorb_fragments(build_turns(alternating(4, chars=2, step=0.4)))
    assert len(four) == 4                     # untouched
    five = absorb_fragments(build_turns(alternating(5, chars=2, step=0.4)))
    assert len(five) < 5                      # collapsed
    assert TINY_CHAIN_MIN == 5


def test_chain_fold_keeps_the_text_in_order_and_respects_the_caps():
    words = alternating(9, chars=2, step=0.3)
    runs = absorb_fragments(build_turns(words), max_duration_s=12.0, max_chars=60)
    text = "".join("".join(w.text for w in run.words) for run in runs)
    assert text == "".join(w.text for w in words)     # nothing lost, no reorder
    assert len(runs) >= 1
    for run in runs:
        assert (run.words[-1].end - run.words[0].start) <= 12.0
        assert len("".join(w.text for w in run.words)) <= 60


def test_a_real_pause_stops_absorption():
    """A short reply isolated by real silence keeps its own line."""
    words = [W("ああ", 0.0, 0.5), W("はい", 2.0, 2.3, "2")]
    runs = absorb_fragments(build_turns(words))
    assert len(runs) == 2


def test_the_chain_rule_turns_the_jitter_passage_into_readable_lines():
    """The reported passage: 13 one-to-three-character turns over 16 s, all
    adjacent, labels flipping every word. It must not become 13 subtitles."""
    words = []
    t = 0.0
    pieces = ["爆乳", "彼女", "と隣", "の", "爆乳", "お姉さんの", "彼氏を",
              "かけた", "奪い", "合い", "レズバ", "ト", "ル"]
    for i, p in enumerate(pieces):
        for ch in p:
            words.append(W(ch, t, t + 0.08, "1" if i % 2 == 0 else "2"))
            t += 0.08
        t += 0.12
    runs = absorb_fragments(build_turns(words), max_duration_s=12.0, max_chars=60)
    assert 1 <= len(runs) <= 4                       # was 13
    assert all(len("".join(w.text for w in r.words)) >= 2 for r in runs)


# ------------------------------------------------------------ silence fuse

def _entry(start, end, text, words, speakers={"1"}):
    return SubEntry(start, end, text, set(speakers), words=list(words))


def test_silence_fuse_cuts_a_line_spanning_dead_air():
    words = [W("ああ", 0.0, 0.5), W("いい", 0.6, 1.0), W("うう", 5.0, 5.5)]
    e = _entry(0.0, 5.5, "ああいいうう", words)
    logs = []
    out = _silence_split([e], PostprocessParams(), logs.append)
    assert len(out) == 2
    assert out[0].text == "ああいい" and out[1].text == "うう"
    assert out[0].end == 1.0 and out[1].start == 5.0     # honest word times
    assert any("长静默拆行" in m for m in logs)


def test_silence_fuse_leaves_short_pauses_alone():
    words = [W("ああ", 0.0, 0.5), W("いい", 3.0, 3.4)]     # 2.5 s < 3 s
    e = _entry(0.0, 3.4, "ああいい", words)
    out = _silence_split([e], PostprocessParams(), lambda m: None)
    assert len(out) == 1


def test_silence_fuse_never_cuts_a_dialogue_line():
    words = [W("ああ", 0.0, 0.5), W("いい", 5.0, 5.5)]
    e = _entry(0.0, 5.5, "- ああ\n- いい", words, speakers={"1", "2"})
    e.is_dialogue = True
    out = _silence_split([e], PostprocessParams(), lambda m: None)
    assert len(out) == 1


def test_one_character_line_folds_into_its_neighbour():
    entries = [
        _entry(0.0, 1.0, "お姉さんの", [W("お", 0.0, 1.0)]),
        _entry(1.1, 1.2, "の", [W("の", 1.1, 1.2)]),
        _entry(1.9, 2.4, "彼氏を", [W("彼", 1.9, 2.4, "2")]),
    ]
    out = _fold_tiny(entries, PostprocessParams(), lambda m: None)
    assert len(out) == 2
    assert out[0].text == "お姉さんのの"        # folded into the closer neighbour
    assert out[1].text == "彼氏を"


def test_one_character_line_with_no_close_neighbour_stands_alone():
    entries = [
        _entry(0.0, 1.0, "お姉さんの", [W("お", 0.0, 1.0)]),
        _entry(3.0, 3.1, "の", [W("の", 3.0, 3.1)]),
    ]
    out = _fold_tiny(entries, PostprocessParams(), lambda m: None)
    assert len(out) == 2


# ------------------------------------------------------------ end to end

def test_end_to_end_no_fragment_lines_no_pushed_timeline():
    """One pass over a small transcript with all three problems at once."""
    words = []
    t = 0.0
    # 6 adjacent 1-2 char turns: jitter
    for i in range(6):
        words.append(W("あ" * (1 + i % 2), t, t + 0.2, "1" if i % 2 == 0 else "2"))
        t += 0.25
    # then a line whose words straddle a 4 s silence
    words.append(W("いい", t, t + 0.4))
    words.append(W("うう", t + 4.4, t + 4.8))
    transcript = Transcript(words=words, duration_s=t + 5.0, language="ja")
    segs = segment_transcript(transcript, SegmentParams(), log=lambda m: None)
    pp = PostprocessParams()
    out = process(segs, pp, audio_duration_s=t + 5.0, split_params=SegmentParams(),
                  log=lambda m: None)
    assert all(len(e.text) >= 2 for e in out)                 # no 1-char lines
    for e in out:
        gaps = [e.words[i + 1].start - e.words[i].end
                for i in range(len(e.words) - 1)]
        assert not gaps or max(gaps) < 3.0 or e.is_dialogue   # no dead-air lines
    # the timeline is the words' timeline: every start equals its first word's
    for e in out:
        assert e.start <= e.words[0].start + 0.01
