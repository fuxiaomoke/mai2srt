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

    def fake_request(endpoint, system, user):
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

    def fake_request(endpoint, system, user):
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

    def fake_request(endpoint, system, user):
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

    def fake_request(endpoint, system, user):
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
