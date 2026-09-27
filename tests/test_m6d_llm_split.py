# -*- coding: utf-8 -*-
"""M6f LLM-split tests: free-text line task shape + exact alignment.

Covers: the rewritten prompt (verbatim hard constraint, target range,
language-agnostic sentence finals), retry policy, summary context, partial
degradation, and the deterministic line->word-span alignment."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.segment import llm
from mai2srt.segment.candidates import find_candidates
from mai2srt.segment.llm import (
    LLMSplitError, LLMEndpoint, ProtocolSplitter, SplitTask, _SYSTEM_PROMPT,
    _TARGET_MIN_CHARS, _align_lines, _is_retryable_error,
)
from mai2srt.segment.rules import SegmentParams, join_words
from mai2srt.transcribe.parser import Word


def _task(run_id: int, text: str = "あいう。かきく。さしす。"):
    words = [Word(ch, i * 0.2, i * 0.2 + 0.15, "1") for i, ch in enumerate(text)]
    return SplitTask(run_id, words, find_candidates(words, 0.8))


def _blocks(user: str) -> list[tuple[int, str]]:
    """Parse the [id=N] blocks back out of a split request."""
    out = []
    for part in user.split("\n[id=")[1:]:
        head, _, tail = part.partition("]\n")
        out.append((int(head), tail.strip("\n")))
    return out


def _echo(user: str) -> str:
    """A reply that returns every block verbatim as a single line."""
    return '{"results": [%s]}' % ", ".join(
        '{"id": %d, "lines": ["%s"]}' % (rid, txt) for rid, txt in _blocks(user))


# ------------------------------------------------------------------ prompt

def test_prompt_free_text_shape_and_range():
    p = _SYSTEM_PROMPT.format(min_chars=_TARGET_MIN_CHARS, max_chars=60)
    # verbatim hard constraint (trans-jimaku-web's load-bearing rule)
    assert "不得增加、删除或改写任何字符" in p
    assert "完全一致" in p
    # target RANGE + explicit merge instruction (anti-fragmentation)
    assert "%d～%d" % (_TARGET_MIN_CHARS, 60) in p
    assert "连续的短句可酌情合并" in p
    assert _TARGET_MIN_CHARS == 10
    # reply contract: complete lines, not candidate numbers
    assert '"lines"' in p
    assert "splits" not in p


def test_prompt_is_language_agnostic_on_sentence_finals():
    p = _SYSTEM_PROMPT.format(min_chars=10, max_chars=60)
    # the rule's SUBJECT is "the text language's sentence-final marks",
    # with CJK/latin spellings as EXAMPLES only (user requirement)
    assert "当前文本所用语言的常用句尾标点" in p
    assert "「。」「？」「！」" in p          # CJK examples
    assert ". ? !" in p                      # latin examples
    assert "个字符" in p                     # unit is characters, not 字


# --------------------------------------------------------------- alignment

def test_align_lines_exact_and_whitespace_tolerant():
    t = _task(1, "あいう。かきく。")
    # model inserted stray spaces between CJK chars -> still aligns
    pieces = _align_lines(["あい う。", "かきく。"], t.words)
    assert [join_words(pc) for pc in pieces] == ["あいう。", "かきく。"]


def test_align_lines_mismatch_raises():
    t = _task(1, "あいう。かきく。")
    try:
        _align_lines(["あいう。", "かきこ。"], t.words)   # model rewrote a char
        raised = False
    except LLMSplitError as e:
        raised = True
        assert "mismatch" in str(e)           # retry marker present
    assert raised


def test_align_lines_uncovered_tail_raises():
    t = _task(1, "あいう。かきく。")
    try:
        _align_lines(["あいう。"], t.words)    # second sentence never claimed
        raised = False
    except LLMSplitError as e:
        raised = True
        assert "mismatch" in str(e)
    assert raised


# ----------------------------------------------------------------- retry

def test_is_retryable_error_policy():
    assert _is_retryable_error("HTTP 429 from x: rate") is True
    assert _is_retryable_error("HTTP 503 from x: down") is True
    assert _is_retryable_error("HTTP 401 from x: bad key") is False
    assert _is_retryable_error("HTTP 400 from x: bad request") is False
    assert _is_retryable_error("request to x failed: timeout") is True
    assert _is_retryable_error("no JSON object in LLM reply") is True
    assert _is_retryable_error("line mismatch: line 2 does not match") is True


def test_choose_partial_failure_returns_partial(monkeypatch):
    """One task keeps failing -> only that task degrades; the other task's
    aligned pieces survive."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    sp = ProtocolSplitter(LLMEndpoint(api_key="k", model="m"))

    calls = {"summary": 0, "split": 0}

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            calls["summary"] += 1
            return "摘要：测试。"
        calls["split"] += 1
        if "[id=2]" in user:
            return "not json at all"      # unparseable -> exhausts retries
        return '{"results": [{"id": 1, "lines": ["あいう。", "かきく。さしす。"]}]}'

    monkeypatch.setattr(llm, "_request", fake_request)
    out = sp.choose([_task(1), _task(2)], SegmentParams())
    assert list(out.keys()) == [1]                 # task 2 degraded
    assert [join_words(pc) for pc in out[1]] == ["あいう。", "かきく。さしす。"]
    assert calls["summary"] == 1
    assert calls["split"] >= 3                     # retries before giving up


def test_choose_total_failure_raises(monkeypatch):
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    sp = ProtocolSplitter(LLMEndpoint(api_key="k", model="m"))

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        return "garbage"

    monkeypatch.setattr(llm, "_request", fake_request)
    try:
        sp.choose([_task(1)], SegmentParams())
        raised = False
    except LLMSplitError:
        raised = True
    assert raised


def test_choose_summary_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    logs: list[str] = []
    sp = ProtocolSplitter(LLMEndpoint(api_key="k", model="m"), log=logs.append)

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            raise LLMSplitError("HTTP 500 from x: boom")
        # split request must arrive WITHOUT the summary block
        assert "【全文摘要】" not in user
        return '{"results": [{"id": 1, "lines": ["あいう。かきく。さしす。"]}]}'

    monkeypatch.setattr(llm, "_request", fake_request)
    out = sp.choose([_task(1)], SegmentParams())
    assert [join_words(pc) for pc in out[1]] == ["あいう。かきく。さしす。"]
    assert any("摘要生成失败" in m for m in logs)


def test_choose_misaligned_reply_is_not_returned(monkeypatch):
    """A reply whose lines do not reconstruct the source is rejected (with
    retries); it must never yield wrong timestamps."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    sp = ProtocolSplitter(LLMEndpoint(api_key="k", model="m"))

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        # silently drops the last sentence -> alignment coverage failure
        return '{"results": [{"id": 1, "lines": ["あいう。"]}]}'

    monkeypatch.setattr(llm, "_request", fake_request)
    try:
        sp.choose([_task(1)], SegmentParams())
        raised = False
    except LLMSplitError:
        raised = True
    assert raised


def test_thinking_form_400_flips_to_effort_and_retries(monkeypatch):
    """Claude 4.7+ reject thinking.type=enabled with a 400. A relay that
    renamed its models hides that from the name heuristics -- the splitter
    must flip the parameter form on that 400 and retry the batch instead
    of degrading it."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    logs: list[str] = []
    sp = ProtocolSplitter(
        LLMEndpoint(protocol="anthropic", api_key="k", effort="high",
                    model="claude-relay-alias"),   # unparseable -> budget
        log=logs.append)
    seen: list[str] = []

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        seen.append(endpoint.resolve_thinking_mode())
        if len(seen) == 1:
            raise LLMSplitError(
                'HTTP 400 from x: "thinking.type.enabled" is not supported')
        return _echo(user)

    monkeypatch.setattr(llm, "_request", fake_request)
    out = sp.choose([_task(1)], SegmentParams())
    assert seen == ["budget", "effort"]           # flipped, then succeeded
    assert sp.e.thinking_mode == "effort"         # remembered for the job
    assert any("思考参数形式" in m for m in logs)
    assert list(out.keys()) == [1]


def test_thinking_form_400_flips_back_to_budget(monkeypatch):
    """The reverse shape also self-corrects: an Opus 4.5-era endpoint (or a
    relay that strips output_config) rejects the effort form -> fall back
    to budget_tokens."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    sp = ProtocolSplitter(
        LLMEndpoint(protocol="anthropic", api_key="k", effort="high",
                    model="claude-opus-4-6"))      # name says effort
    seen: list[str] = []

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        seen.append(endpoint.resolve_thinking_mode())
        if len(seen) == 1:
            raise LLMSplitError(
                "HTTP 400 from x: output_config is not supported by this model")
        return _echo(user)

    monkeypatch.setattr(llm, "_request", fake_request)
    out = sp.choose([_task(1)], SegmentParams())
    assert seen == ["effort", "budget"]
    assert sp.e.thinking_mode == "budget"
    assert list(out.keys()) == [1]


def test_gemini_thinking_400_drops_thinking_and_retries(monkeypatch):
    """A level the model does not take (3-series Pro accepts low/high only,
    a renamed relay alias hides that from the name) kills thinkingConfig
    for the rest of the job instead of degrading the batch."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    logs: list[str] = []
    sp = ProtocolSplitter(
        LLMEndpoint(protocol="gemini", api_key="k", effort="medium",
                    model="gemini-relay-alias"),
        log=logs.append)
    sent: list[dict] = []

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        sent.append(endpoint.effort_kwargs())
        if len(sent) == 1:
            raise LLMSplitError(
                "HTTP 400: Invalid thinkingLevel medium. "
                "Valid values are: low, high")
        return _echo(user)

    monkeypatch.setattr(llm, "_request", fake_request)
    out = sp.choose([_task(1)], SegmentParams())
    assert sent[0] != {} and sent[1] == {}       # thinking, then dropped
    assert sp.e.thinking_mode == "none"
    assert any("已关闭思考" in m for m in logs)
    assert list(out.keys()) == [1]


def test_openai_reasoning_effort_400_drops_it_and_retries(monkeypatch):
    """Not every openai-compatible backend takes reasoning_effort; one 4xx
    naming the field drops it for the rest of the job instead of letting
    the batch degrade to rule-based splitting."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    sp = ProtocolSplitter(
        LLMEndpoint(protocol="openai", api_key="k", effort="high",
                    model="some-relay-model"))
    sent: list[dict] = []

    def fake_request(endpoint, system, user, **_kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        sent.append(endpoint.effort_kwargs())
        if len(sent) == 1:
            raise LLMSplitError(
                "HTTP 400: Unrecognized request argument supplied: "
                "reasoning_effort")
        return _echo(user)

    monkeypatch.setattr(llm, "_request", fake_request)
    out = sp.choose([_task(1)], SegmentParams())
    assert sent[0] == {"reasoning_effort": "high"} and sent[1] == {}
    assert sp.e.thinking_mode == "none"
    assert list(out.keys()) == [1]


# ------------------------------------------------- generation-cap batching

def test_output_cap_only_for_batches_that_need_one(monkeypatch):
    """A small reply rides the platform default; a batch big enough to outgrow
    it asks for an explicit ceiling (the reply is the same text re-emitted)."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    caps: list = []

    def run_one(text: str):
        caps.clear()
        sp = ProtocolSplitter(LLMEndpoint(api_key="k", model="m"))

        def fake_request(endpoint, system, user, **kw):
            if system == llm._SUMMARY_SYSTEM_PROMPT:
                return "摘要。"
            caps.append(kw.get("output_tokens"))
            return '{"results": [{"id": 1, "lines": ["%s"]}]}' % text

        monkeypatch.setattr(llm, "_request", fake_request)
        return sp.choose([_task(1, text)], SegmentParams())

    run_one("あいう。かきく。")
    assert caps == [None]                      # 25 tokens expected: send nothing

    text = "あ" * 6000                         # ~3.6K text + scaffolding: over the floor
    out = run_one(text)
    need = int((len(text) * llm._CHARS_PER_TOKEN + llm._TASK_OVERHEAD_TOKENS)
               * llm._JSON_OVERHEAD)
    assert caps == [need]
    assert caps[0] > llm._OUTPUT_CAP_FLOOR_TOKENS
    assert [join_words(pc) for pc in out[1]] == [text]


def test_output_cap_rejection_retries_without_and_shrinks(monkeypatch):
    """An endpoint that refuses the cap field is not a failure: the field is
    dropped for the rest of the job and later batches shrink to a ceiling a
    platform default is known to cover."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    logs: list[str] = []
    sp = ProtocolSplitter(LLMEndpoint(api_key="k", model="m"), log=logs.append)
    text = "あ" * 6000
    caps: list = []

    def fake_request(endpoint, system, user, **kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        caps.append(kw.get("output_tokens"))
        if kw.get("output_tokens"):
            raise LLMSplitError("HTTP 400 from x: Unsupported parameter: "
                                "max_completion_tokens")
        return '{"results": [{"id": 1, "lines": ["%s"]}]}' % text

    monkeypatch.setattr(llm, "_request", fake_request)
    out = sp.choose([_task(1, text)], SegmentParams())

    assert caps[0] and caps[1] is None          # asked, refused, retried bare
    assert [join_words(pc) for pc in out[1]] == [text]    # still succeeded
    assert any("端点不接受生成上限参数" in m for m in logs)
    assert sp._cap_chars == int(llm._ASSUMED_DEFAULT_OUTPUT_TOKENS
                                * llm._OUTPUT_SAFETY / llm._CHARS_PER_TOKEN)
    # the tighter ceiling now governs packing
    many = [_task(i, "あ" * 61 + "。") for i in range(300)]
    batches = sp._batches(many)
    assert len(batches) > 1
    assert all(sum(len(join_words(t.words)) for t in b) <= sp._cap_chars
               for b in batches)


# ------------------------------------------------------- progress reporting

def test_every_batch_and_the_total_are_logged(monkeypatch):
    """A multi-batch job runs for minutes: it must narrate itself instead of
    leaving the UI log blank between "摘要生成成功" and the end."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    logs: list[str] = []
    sp = ProtocolSplitter(LLMEndpoint(api_key="k", model="m"), log=logs.append)

    def fake_request(endpoint, system, user, **kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            return "摘要。"
        return _echo(user)

    monkeypatch.setattr(llm, "_request", fake_request)
    tasks = [_task(i, "あ" * 61 + "。") for i in range(700)]   # 43_400 chars
    out = sp.choose(tasks, SegmentParams())

    assert len(out) == 700
    assert any("断句：700 段，分 2 批" in m for m in logs)
    assert any("断句批次 1/2" in m for m in logs)
    assert any("断句批次 2/2" in m for m in logs)
    assert any(m.startswith("断句完成：700/700") for m in logs)


def test_summary_input_is_trimmed_to_the_context_budget(monkeypatch):
    """The summary call is NOT batched, so a long job must be trimmed to the
    prompt budget rather than failing and silently losing the context."""
    monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", (0.0, 0.0))
    logs: list[str] = []
    ep = LLMEndpoint(api_key="k", model="m", context_window=8_000)   # 5333 chars
    sp = ProtocolSplitter(ep, log=logs.append)
    seen: dict = {}

    def fake_request(endpoint, system, user, **kw):
        if system == llm._SUMMARY_SYSTEM_PROMPT:
            seen["summary"] = user
            return "摘要。"
        return _echo(user)

    monkeypatch.setattr(llm, "_request", fake_request)
    sp.choose([_task(i, "あ" * 600) for i in range(20)], SegmentParams())

    assert ep.input_budget_chars() == int(8_000 * 0.4 / 0.6)
    assert len(seen["summary"]) <= ep.input_budget_chars() + 8   # "\n...\n"
    assert seen["summary"].startswith("あ")
    assert seen["summary"].rstrip().endswith("あ")
    assert any("超出输入预算" in m for m in logs)


def test_partial_split_is_reported_through_llm_status():
    """A run the splitter never answers for falls back deterministically in
    the RESULT -- and the user must be told, not just the log."""
    from mai2srt.segment.pipeline import segment_transcript
    from mai2srt.transcribe.parser import Transcript

    words: list[Word] = []
    t0 = 0.0
    for spk in ("1", "2"):                      # two over-limit runs
        for k in range(80):
            words.append(Word("あ", t0 + k * 0.2, t0 + k * 0.2 + 0.15, spk))
        t0 += 20.0
    t = Transcript(words=words, duration_s=40.0, language="ja")

    class HalfSplitter:
        """Answers for the first task only -- the second one degrades."""

        def choose(self, tasks, params):
            first = tasks[0]
            return {first.run_id: [list(first.words)]}

    status: list[str] = []
    segs = segment_transcript(t, SegmentParams(), splitter=HalfSplitter(),
                              llm_status=status)
    assert status and "1/2" in status[0]
    assert "未能对齐" in status[0]
    assert len(segs) >= 2                       # both runs still produced lines


def test_total_split_success_reports_nothing():
    from mai2srt.segment.pipeline import segment_transcript
    from mai2srt.transcribe.parser import Transcript

    words = [Word("あ", i * 0.2, i * 0.2 + 0.15, "1") for i in range(80)]
    t = Transcript(words=words, duration_s=20.0, language="ja")

    class AllSplitter:
        def choose(self, tasks, params):
            return {t_.run_id: [list(t_.words)] for t_ in tasks}

    status: list[str] = []
    segment_transcript(t, SegmentParams(), splitter=AllSplitter(),
                       llm_status=status)
    assert status == []
