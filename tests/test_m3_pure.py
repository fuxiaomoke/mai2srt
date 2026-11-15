"""M3 post-processing unit tests (pure functions, no network)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.postprocess import (
    PostprocessParams,
    SubEntry,
    can_merge,
    detect_dialogue,
    expand_outward,
    final_format,
    merge_benefit,
    smart_merge,
)
from mai2srt.postprocess.pipeline import process
from mai2srt.segment.pipeline import Segment, segment_transcript
from mai2srt.segment.rules import SegmentParams
from mai2srt.srt import format_ts, to_srt
from mai2srt.transcribe.parser import Transcript, Word


def W(text, start, end, speaker):
    return Word(text, start, end, speaker)


def E(start, end, text, speakers, **kw):
    return SubEntry(start, end, text, speakers, **kw)


# ---------------------------------------------------------------- dialogue
# 2026-11-15: the trigger is MAI's word-order interleave (X Y X), not
# "adjacent + different label". The old rule was measured against ground truth
# on a 55-min source: 31/31 of its dialogue lines were one sentence cut in half
# (the worst passage is a deliberate A/B duet read where the diarizer puts the
# handoffs one word off), and it regrouped the text by speaker, scrambling it.

def test_dialogue_needs_the_xyx_return():
    # A -> B -> A, all adjacent: the speaker RETURNS, so this is an interleave
    entries = [
        E(0.64, 0.90, "あなた様。", {"1"}),
        E(0.92, 1.82, "あなた様。", {"2"}),
        E(1.86, 2.60, "そうですわね。", {"1"}),
    ]
    out = detect_dialogue(entries)
    assert len(out) == 1
    d = out[0]
    assert d.is_dialogue and d.start == 0.64 and d.end == 2.60
    # per-speaker grouping is CORRECT for a real interleave
    assert d.text == "- あなた様。そうですわね。\n- あなた様。"


def test_dialogue_plain_exchange_is_two_lines_not_a_pair():
    """The deleted rule: adjacent different speakers alone are NOT dialogue.

    Two utterances in a row is ordinary turn-taking -- merging them was the
    fake-dialogue generator this pass removed."""
    entries = [
        E(0.64, 0.90, "あなた様。", {"1"}),
        E(0.92, 1.82, "あなた様。", {"2"}),
    ]
    out = detect_dialogue(entries)
    assert len(out) == 2 and not any(e.is_dialogue for e in out)


def test_dialogue_gap_over_tolerance_stays_solo():
    entries = [
        E(0.0, 1.9, "A1", {"1"}),
        E(2.6, 4.0, "B1", {"2"}),     # 0.7 s gap > 0.2 tolerance
        E(4.3, 6.0, "A2", {"1"}),     # 0.3 s gap: still no adjacency, no seed
    ]
    out = detect_dialogue(entries, tolerance_s=0.2)
    assert not any(e.is_dialogue for e in out)


def test_dialogue_xyxy_chain_grows():
    # X Y X Y: the returning speaker seeds the region, the fourth line joins it
    entries = [
        E(0.0, 2.0, "A1", {"A"}),
        E(2.05, 4.0, "B1", {"B"}),
        E(4.1, 6.0, "A2", {"A"}),
        E(6.05, 8.0, "B2", {"B"}),
    ]
    out = detect_dialogue(entries, tolerance_s=0.2)
    assert len(out) == 1
    assert out[0].text == "- A1 A2\n- B1 B2"


def test_dialogue_same_speaker_never_merges():
    entries = [
        E(0.0, 2.0, "A1", {"1"}),
        E(2.05, 4.0, "A2", {"1"}),
    ]
    out = detect_dialogue(entries)
    assert len(out) == 2 and not any(e.is_dialogue for e in out)


def test_dialogue_mixed_speaker_line_never_seeds():
    """A line whose words carry two labels is a diarization accident: it cannot
    stand in for either voice, so it neither seeds nor joins a region."""
    entries = [
        E(0.0, 2.0, "A", {"1"}),
        E(2.05, 4.0, "B", {"2"}),
        E(4.1, 6.0, "AB", {"1", "2"}),
    ]
    out = detect_dialogue(entries, tolerance_s=0.2)
    assert not any(e.is_dialogue for e in out)


def test_dialogue_none_speaker_never_joins():
    entries = [
        E(0.0, 2.0, "A", {"1"}),
        E(2.05, 4.0, "B", {None}),
        E(4.1, 6.0, "C", {"1"}),
    ]
    out = detect_dialogue(entries)
    assert not any(e.is_dialogue for e in out)


def test_dialogue_xyzx_needs_three_speakers_allowed():
    """X Y Z X (three voices) seeds only when the cap allows three."""
    entries = [
        E(0.0, 2.0, "A", {"1"}),
        E(2.05, 4.0, "B", {"2"}),
        E(4.05, 6.0, "C", {"3"}),
        E(6.05, 8.0, "A2", {"1"}),
    ]
    out = detect_dialogue(entries, tolerance_s=0.2, max_speakers=2, log=lambda m: None)
    assert not any(e.is_dialogue for e in out)
    out = detect_dialogue(entries, tolerance_s=0.2, max_speakers=3, log=lambda m: None)
    assert len(out) == 1 and out[0].is_dialogue


def test_dialogue_caps_refuse_a_giant_region():
    logs = []
    entries = [
        E(0.0, 6.0, "A" * 40, {"1"}),
        E(6.05, 12.0, "B" * 40, {"2"}),
        E(12.05, 18.0, "C" * 40, {"1"}),
    ]
    out = detect_dialogue(entries, tolerance_s=0.2, max_chars=100, log=logs.append)
    assert not any(e.is_dialogue for e in out)
    assert any("over the 100-char cap" in m for m in logs)
    out = detect_dialogue(entries, tolerance_s=0.2, max_duration_s=10.0,
                          max_chars=1000, log=logs.append)
    assert not any(e.is_dialogue for e in out)
    assert any("over the 10.0s cap" in m for m in logs)


# ---------------------------------------------------------------- merge

def _mp(**kw):
    d = dict(max_duration_s=12.0, max_chars=60, min_duration_s=1.2,
             merge_gap_threshold_s=0.8, cps_cjk=13.0, cps_other=18.0,
             benefit_threshold=5.0)
    d.update(kw)
    return PostprocessParams(**d)


def test_can_merge_veto_chain():
    p = _mp()
    # disjoint speakers veto (top)
    assert not can_merge(E(0, 1, "あ", {"1"}), E(1.1, 2, "い", {"2"}), p)
    # dialogue entries veto
    a = E(0, 1, "あ", {"1"}); b = E(1.1, 2, "い", {"1"}); b.is_dialogue = True
    assert not can_merge(a, b, p)
    # gap over threshold veto
    assert not can_merge(E(0, 1, "あ", {"1"}), E(2.0, 3, "い", {"1"}), p)
    # duration over max veto
    assert not can_merge(E(0, 8, "あ", {"1"}), E(8.5, 12.1, "い", {"1"}), p)
    # text over max chars veto
    assert not can_merge(E(0, 1, "あ" * 40, {"1"}), E(1.1, 2, "い" * 25, {"1"}), p)
    # CPS veto: too much text, too little time
    assert not can_merge(E(0, 0.5, "あ" * 20, {"1"}), E(0.6, 1.0, "い" * 20, {"1"}), p)
    # sane pair passes
    assert can_merge(E(0, 1.0, "こんにちは", {"1"}), E(1.1, 2.2, "世界", {"1"}), p)


def test_merge_benefit_scores():
    p = _mp()
    # both short + tiny gap + short texts -> large benefit
    hi = merge_benefit(E(0, 0.5, "うん", {"1"}), E(0.6, 1.0, "え", {"1"}), p)
    assert hi > 5.0
    # both long, clear gap, long texts -> zero benefit
    lo = merge_benefit(E(0, 5, "あ" * 30, {"1"}), E(6.0, 11.0, "い" * 30, {"1"}), p)
    assert lo == 0.0


def test_smart_merge_greedy_pairs():
    p = _mp()
    entries = [
        E(0, 0.5, "うん", {"1"}),
        E(0.6, 1.0, "えっと", {"1"}),
        E(1.1, 6.0, "本題に入ります。", {"1"}),
    ]
    out = smart_merge(entries, p)
    # first pair merges (benefit > 5), third stays
    assert len(out) == 2
    assert out[0].text == "うんえっと"


# ---------------------------------------------------------------- timeline

def test_expand_outward_shared_boundary_never_overlaps():
    p = PostprocessParams(mai_expand_s=0.25, min_gap_s=0.1)
    entries = [
        E(1.0, 2.0, "A", {"1"}),
        E(2.6, 3.6, "B", {"1"}),     # 0.6 s gap; spare = 0.5, side = 0.25 each
    ]
    expand_outward(entries, p, audio_duration_s=10.0)
    # A: start 0.75 (1.0-0.25), end 2.0+min(0.25,0.25)=2.25
    # B: start 2.6-0.25=2.35, end 3.6+0.25=3.85
    assert abs(entries[0].start - 0.75) < 1e-9
    assert abs(entries[0].end - 2.25) < 1e-9
    assert abs(entries[1].start - 2.35) < 1e-9
    assert abs(entries[1].end - 3.85) < 1e-9
    assert entries[1].start - entries[0].end >= p.min_gap_s - 1e-9


def test_expand_outward_clamps_first_last():
    p = PostprocessParams(mai_expand_s=0.25, min_gap_s=0.1)
    entries = [E(0.1, 1.0, "A", {"1"})]
    expand_outward(entries, p, audio_duration_s=1.5)
    assert entries[0].start == 0.0                 # clamped at 0
    assert abs(entries[0].end - 1.25) < 1e-9       # clamped at audio end 1.5


def test_final_format_extends_into_real_silence_only():
    p = PostprocessParams(min_gap_s=0.1, min_duration_s=1.2, min_duration_abs_s=0.5)
    entries = [
        E(0.0, 1.0, "A", {"1"}),
        E(4.0, 4.4, "B", {"1"}),      # plenty of silence before it
    ]
    out = final_format(entries, p)
    assert out[0].start == 0.0                    # start untouched
    assert out[0].end == 1.2                      # extended to the 1.2 s target
    assert out[1].start == 4.0                    # never pushed
    assert out[1].end == 5.2


def test_final_format_never_pushes_a_start():
    """2026-11-15: the old pass moved `cur.start` to `prev.end + min_gap` after
    extending the previous line, so one short line shifted the whole file late
    (measured +4.9 s at 68 s). Starts are now sacred."""
    p = PostprocessParams(min_gap_s=0.1, min_duration_s=1.2, min_duration_abs_s=0.5)
    entries = [
        E(0.0, 1.0, "A", {"1"}),
        E(1.02, 1.2, "B", {"1"}),     # 20 ms after A: no room to extend A
        E(1.25, 1.5, "C", {"1"}),
    ]
    out = final_format(entries, p, audio_duration_s=1.5)
    assert [e.start for e in out] == [0.0, 1.02, 1.25]      # all untouched
    # A cannot reach 1.2 s: 1.02 - 0.1 = 0.92 is the most the silence allows
    assert out[0].end == pytest.approx(0.92)
    assert out[1].end == pytest.approx(1.15)      # 1.25 - 0.1
    assert out[2].end == pytest.approx(1.5)       # clamped at the audio end


def test_final_format_overlap_trims_the_previous_tail():
    p = PostprocessParams(min_gap_s=0.1, min_duration_s=1.2, min_duration_abs_s=0.5)
    entries = [
        E(0.0, 2.0, "A", {"1"}),
        E(1.5, 3.0, "B", {"1"}),      # overlapping start
    ]
    out = final_format(entries, p)
    assert out[1].start == 1.5                    # start kept
    assert out[0].end == pytest.approx(1.4)       # previous tail trimmed


# ---------------------------------------------------------------- srt + e2e

def test_format_ts_and_srt_roundtrip():
    assert format_ts(3661.5) == "01:01:01,500"
    entries = [E(0.0, 2.0, "- A\n- B", {"1", "2"}, is_dialogue=True)]
    srt = to_srt(entries)
    assert "1\n00:00:00,000 --> 00:00:02,000\n- A\n- B\n" in srt


def _dual_transcript():
    # X Y X at the top (three adjacent turns -> one dialogue line), then two
    # long solo stretches separated by real pauses
    words = []
    words += [W("こ", 0.64 + i * 0.08, 0.72 + i * 0.08, "1") for i in range(3)]
    words += [W("ん", 0.92 + i * 0.08, 1.00 + i * 0.08, "2") for i in range(3)]
    words += [W("は", 1.20 + i * 0.08, 1.28 + i * 0.08, "1") for i in range(3)]
    words += [W("い", 3.00 + i * 0.2, 3.20 + i * 0.2, "2") for i in range(6)]
    words += [W("を", 6.00 + i * 0.2, 6.20 + i * 0.2, "1") for i in range(4)]
    words.append(W("。", 6.90, 7.00, "1"))
    return Transcript(words=words, duration_s=8.0, language="ja")


def test_process_end_to_end_dual_speaker():
    t = _dual_transcript()
    segs = segment_transcript(t, SegmentParams(max_chars=8, min_chars=2))
    logs = []
    out = process(segs, audio_duration_s=t.duration_s, log=logs.append)
    # exactly one dialogue group: the X-Y-X interleave at the top
    dials = [e for e in out if e.is_dialogue]
    assert len(dials) == 1
    assert dials[0].text.startswith("- ")
    assert "\n- " in dials[0].text
    # timeline monotonic + no overlap
    for a, b in zip(out, out[1:]):
        assert b.start >= a.start
        assert b.start >= a.end - 0.005      # min-gap enforced (0.01 slack for the overlap-fix push)


# ------------------------------------------------- flagship ports (trans-jimaku-web)

def _w(text, s, e, sp):
    return Word(text, s, e, sp)


def _run_chain(words, audio_dur):
    t = Transcript(words=list(words), duration_s=audio_dur, language="ja")
    segs = segment_transcript(t, SegmentParams(min_chars=0))
    return process(segs, audio_duration_s=audio_dur)


def test_flagship_hora_ya_buracon():
    """trans-jimaku-web flagship user sample: sp0 ほら、 -> sp1 や- ->
    sp0 ブラコンじゃん。 -> sp1 ブラコン。 -- interleaved within tolerance;
    same-speaker runs glue into one line each."""
    words = [
        _w("ほ", 128.88, 128.96, "0"), _w("ら", 129.0, 129.08, "0"),
        _w("、", 129.08, 129.099, "0"),
        _w("や", 129.24, 129.319, "1"), _w("-", 129.4, 129.42, "1"),
        _w("ブ", 129.44, 129.52, "0"), _w("ラ", 129.52, 129.599, "0"),
        _w("コ", 129.639, 129.699, "0"), _w("ン", 129.7, 129.78, "0"),
        _w("じ", 129.92, 129.939, "0"), _w("ゃ", 129.94, 129.979, "0"),
        _w("ん", 129.98, 130.0, "0"), _w("。", 130.0, 130.08, "0"),
        _w("ブ", 130.24, 130.319, "1"), _w("ラ", 130.36, 130.439, "1"),
        _w("コ", 130.52, 130.599, "1"), _w("ン", 130.62, 130.699, "1"),
        _w("。", 130.72, 130.74, "1"),
    ]
    out = _run_chain(words, 530.0)
    dials = [e for e in out if e.is_dialogue]
    assert len(dials) == 1
    assert dials[0].text == "- ほら、ブラコンじゃん。\n- や-ブラコン。"


def test_flagship_tolerance_needed():
    """0.05 s regression gap still hits within the 0.2 s tolerance.

    Two-character turns on purpose: a ONE-character turn is now folded as an
    artifact (turns.absorb_fragments), which would erase the pattern."""
    words = [
        _w("ああ", 10.0, 10.5, "0"),
        _w("いい", 10.55, 11.0, "1"),
        _w("うう", 11.05, 11.5, "0"),
    ]
    out = _run_chain(words, 20.0)
    dials = [e for e in out if e.is_dialogue]
    assert len(dials) == 1
    assert dials[0].text == "- ああうう\n- いい"


def test_flagship_no_trigger_beyond_tolerance():
    """real turn-taking (gaps beyond tolerance) -> three solo lines."""
    words = [
        _w("ああ", 10.0, 10.5, "0"),
        _w("いい", 10.75, 11.0, "1"),
        _w("うう", 11.3, 11.5, "0"),
    ]
    out = _run_chain(words, 20.0)
    assert not any(e.is_dialogue for e in out)
    assert [e.text for e in out] == ["ああ", "いい", "うう"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("[ok] %s" % fn.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            import traceback
            print("[FAIL] %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
