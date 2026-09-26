"""Protocol-aware LLM subtitle segmentation (PLAN 4.1/4.3 + PLAN-UI 4.2/4.4;
M6d hybrid redesign; M6f task-shape change, user decision).

Three protocols (openai-compatible / anthropic / gemini) behind one
``Splitter`` interface, with per-protocol reasoning-effort mapping and
context-window-adaptive batching. Core safety contract since M2 is
unchanged: the LLM never sees timestamps and its output is mapped back onto
WORD BOUNDARIES ONLY, so timestamps stay lossless by construction. Pure
urllib, no new dependency.

M6f task shape (user decision, trans-jimaku-web wisdom): the model RECEIVES
plain text and RETURNS complete subtitle lines (verbatim, punctuation
included) instead of picking candidate numbers. Writing whole sentences
makes it naturally aware of sentence endings -- the numbered-picker task
let the model stay "blind" to text structure and cut rhythmically at
commas. Losslessness is preserved deterministically: the prompt's hard
constraint ("all lines concatenated == the source") makes every line an
exact substring, so alignment back to word spans is exact matching, not
fuzzy (any mismatch -> corrective retry -> per-batch degradation).

M6e note: the deterministic sentence-completion pass was ACTIVATED briefly
and is now HELD INACTIVE in pipeline.py -- punctuation is not a trustworthy
semantic signal across languages (a transcription comma may BE a sentence
end); revisit only as a per-language fallback.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable, Protocol

from ..config import LLMConfig
from ..net import USER_AGENT
from ..transcribe.parser import Word
from .candidates import Candidate
from .rules import SegmentParams, _needs_space, join_words

#: batch guards -- M2 defaults, now clamped by the active model's context
_MAX_TASKS_PER_REQUEST = 20
_MAX_MARKED_CHARS_PER_REQUEST = 40_000

#: M6d: fixed target-range floor (user decision -- trans-jimaku-web's 10~50)
_TARGET_MIN_CHARS = 10

#: M6d: request retry chain (trans-jimaku-web parity)
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 2.0)
_RETRYABLE_HTTP = (408, 409, 425, 429, 500, 502, 503, 504)
_NON_RETRYABLE_HTTP = (400, 401, 403, 404)
_RETRYABLE_KEYWORDS = ("timeout", "timed out", "connect", "connection",
                       "network", "超时", "网络", "reply", "json", "mismatch")

#: conservative chars-per-token estimate for CJK-heavy subtitle text
_CHARS_PER_TOKEN = 0.6


# ---------------------------------------------------------------------------
# endpoint description
# ---------------------------------------------------------------------------

@dataclass
class LLMEndpoint:
    """One fully-resolved endpoint: provider row + model meta + effort."""

    protocol: str = "openai"              # openai | anthropic | gemini
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = ""
    effort: str | None = None             # off | low | medium | high | max | None
    context_window: int | None = None     # tokens
    max_output: int | None = None         # tokens
    temperature: float = 0.0
    timeout_s: float = 120.0

    @classmethod
    def from_legacy(cls, cfg: LLMConfig) -> "LLMEndpoint":
        return cls(protocol="openai", base_url=cfg.base_url, api_key=cfg.api_key,
                   model=cfg.model, temperature=cfg.temperature,
                   timeout_s=cfg.timeout_s)

    def budget_chars(self) -> int:
        """Marked-text char budget per request, clamped by context window."""
        budget = _MAX_MARKED_CHARS_PER_REQUEST
        if self.context_window and self.context_window > 0:
            adaptive = int(self.context_window * 0.4 / _CHARS_PER_TOKEN)
            budget = min(budget, max(2_000, adaptive))
        return budget

    def effort_kwargs(self) -> dict:
        """Protocol-native reasoning effort; empty when off/unsupported."""
        if not self.effort or self.effort == "off":
            return {}
        if self.protocol == "openai":
            # widely-adopted field; models that lack it ignore or reject --
            # a rejection surfaces as a normal LLM error and degrades safely
            return {"reasoning_effort": self.effort}
        if self.protocol == "anthropic":
            budgets = {"low": 1024, "medium": 4096, "high": 16384, "max": 65536}
            # the thinking budget MUST stay strictly under the request's
            # max_tokens -- clamp to what this model actually allows
            # (unknown meta mirrors _build_request's 8192 default); a
            # model with no room for thinking sends no thinking block
            cap = (self.max_output or 8192) - 1024
            budget = min(budgets.get(self.effort, 1024), cap)
            if budget < 1024:
                return {}
            return {"thinking": {"type": "enabled",
                                 "budget_tokens": budget}}
        if self.protocol == "gemini":
            # the thinkingLevel enum tops out at HIGH: "max" maps onto it
            level = "HIGH" if self.effort == "max" else self.effort.upper()
            return {"generationConfig": {"thinkingConfig": {
                "thinkingLevel": level}}}
        return {}


# ---------------------------------------------------------------------------
# task + protocol interface
# ---------------------------------------------------------------------------

@dataclass
class SplitTask:
    """One over-limit run prepared for LLM choice."""

    run_id: int
    words: list[Word]
    candidates: list[Candidate]   # sorted by index

    def marked_text(self) -> str:
        """Display text with ``[n|`` markers inserted at each candidate."""
        no = {c.index: k + 1 for k, c in enumerate(self.candidates)}
        out = ""
        for i, w in enumerate(self.words):
            t = w.text
            if not t:
                continue
            if not out:
                out = t
            elif _needs_space(out[-1], t[0]):
                out += " " + t
            else:
                out += t
            if i in no:
                out += f"[{no[i]}|"
        return out


class Splitter(Protocol):
    """Backend interface -- segment a batch of runs into subtitle lines.

    ``choose`` returns ``{run_id: pieces}`` where pieces are word lists that
    partition that task's words contiguously (the free-text LLM lines are
    aligned back to exact word spans inside the splitter). Missing ids fall
    back to the deterministic split inside the pipeline.
    """

    def choose(self, tasks: list[SplitTask], params: SegmentParams) -> dict[int, list[list[Word]]]:
        ...  # pragma: no cover


class LLMSplitError(RuntimeError):
    pass


def _is_retryable_error(err: str) -> bool:
    """Retry policy (M6d): transport errors (timeout/connection) and parse
    failures retry with backoff; auth-class 4xx fails fast; other HTTP
    codes are conservatively non-retryable."""
    m = re.match(r"HTTP (\d+)", err or "")
    if m:
        code = int(m.group(1))
        if code in _NON_RETRYABLE_HTTP:
            return False
        return code in _RETRYABLE_HTTP
    low = (err or "").lower()
    return any(k in low for k in _RETRYABLE_KEYWORDS)


# ---------------------------------------------------------------------------
# free-text line alignment (M6f: exact, not fuzzy)
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Whitespace-insensitive comparison form (the model may insert spaces
    between CJK characters; content must otherwise be verbatim)."""
    return re.sub(r"\s+", "", s or "")


def _align_lines(lines: list[str], words: list[Word]) -> list[list[Word]]:
    """Map the model's subtitle lines back onto contiguous word spans.

    The prompt's hard constraint ("all lines concatenated == the source")
    makes each line an exact substring of the token-glued text, so alignment
    is deterministic: extend a candidate span word by word until its glued
    text equals the line. Any drift raises LLMSplitError (message carries
    the 'mismatch' retry marker) -- the batch is retried with a corrective
    note, then degraded. Timestamps can never be invented or lost here.
    """
    pieces: list[list[Word]] = []
    i = 0
    for n, ln in enumerate(lines, 1):
        target = _norm(ln)
        if not target:
            continue                      # ignore blank lines
        start = i
        while i < len(words):
            cand = _norm(join_words(words[start:i + 1]))
            if cand == target:
                pieces.append(words[start:i + 1])
                i += 1
                break
            if len(cand) > len(target):
                raise LLMSplitError(
                    "line mismatch: line %d does not match the source text "
                    "(got ...%s, want ...%s)" % (n, cand[-12:], target[-12:]))
            i += 1
        else:
            raise LLMSplitError(
                "line mismatch: line %d ran past the end of the segment" % n)
    if i != len(words):
        raise LLMSplitError(
            "line mismatch: lines do not cover the whole segment "
            "(%d words unclaimed)" % (len(words) - i))
    return pieces


_SUMMARY_SYSTEM_PROMPT = (
    "请为以下文本生成一段简短的内容摘要（3-5句话），概括说话场景、主要话题和"
    "涉及的人物。摘要仅用于辅助后续断句任务的上下文理解。")

_SYSTEM_PROMPT = """你是字幕断句助手。输入是若干段语音转录文本。请把每段文本分割为字幕行，并输出每行的完整文本。

【硬性约束】
- 不得增加、删除或改写任何字符；每段的所有行按顺序拼接后，必须与该段原文完全一致
- 只调整断行位置，不得调换顺序、不得跨段拼接

【断句原则】
- 以当前文本所用语言的常用句尾标点（如中/日文的「。」「？」「！」、西文的 . ? ! 以及其他语言对应的句尾符号）作为主要分割点，让每行以完整的句子收尾
- 连续的短句可酌情合并成一行；每行建议落在 {min_chars}～{max_chars} 个字符之间，超过 {max_chars} 个字符必须拆分
- 保持口语的自然节奏，不在意群中间强行切断；语气词不孤立成行，下一句的接头词不要挂到上一行末尾
- 音频事件标注（如「(笑声)」「<music>」）若独立出现，让它单独成行

你还会收到一份【全文摘要】帮助理解语境，但断句必须严格基于【当前文本块】，不得引用或修改摘要内容。

只返回 JSON，格式 {{"results": [{{"id": 段编号, "lines": ["第一行文本", "第二行文本", ...]}}, ...]}}，不要输出任何其他文字。"""


# ---------------------------------------------------------------------------
# reply parsing (unchanged since M2)
# ---------------------------------------------------------------------------

def _extract_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    depth, start = 0, -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except ValueError:
                    pass
                start = -1
    raise LLMSplitError("no JSON object in LLM reply")


def _content(reply: dict, protocol: str) -> str:
    try:
        if protocol == "openai":
            return reply["choices"][0]["message"].get("content") or ""
        if protocol == "anthropic":
            return "".join(b.get("text", "") for b in reply["content"]
                           if b.get("type") == "text")
        if protocol == "gemini":
            return reply["candidates"][0]["content"]["parts"][0].get("text", "")
    except (KeyError, IndexError, TypeError):
        pass
    return ""


# ---------------------------------------------------------------------------
# request building (three protocols)
# ---------------------------------------------------------------------------

def _build_request(endpoint: LLMEndpoint, system: str,
                   user: str) -> tuple[str, dict, dict]:
    """Pure request construction -> (url, payload, headers) per protocol."""
    p = endpoint.protocol
    if p == "openai":
        base = endpoint.base_url.rstrip("/")
        url = base + ("/chat/completions" if base.endswith("/v1")
                      else "/v1/chat/completions")
        payload: dict = {
            "model": endpoint.model,
            "temperature": endpoint.temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        payload.update(endpoint.effort_kwargs())
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer %s" % endpoint.api_key}
    elif p == "anthropic":
        url = endpoint.base_url.rstrip("/") + "/v1/messages"
        payload = {
            "model": endpoint.model,
            "max_tokens": endpoint.max_output or 8192,
            "temperature": endpoint.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        payload.update(endpoint.effort_kwargs())
        # Bearer is Anthropic's documented primary auth header and
        # x-api-key their legacy fallback, so sending BOTH keeps official
        # Anthropic happy while Bearer-only Anthropic-format gateways
        # (OpenRouter https://openrouter.ai/api, DeepSeek
        # https://api.deepseek.com/anthropic) authenticate too.
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer %s" % endpoint.api_key,
                   "x-api-key": endpoint.api_key,
                   "anthropic-version": "2023-06-01"}
    elif p == "gemini":
        url = "%s/v1beta/models/%s:generateContent?key=%s" % (
            endpoint.base_url.rstrip("/"), endpoint.model, endpoint.api_key)
        gen: dict = {"temperature": endpoint.temperature}
        gen.update(endpoint.effort_kwargs().get("generationConfig", {}))
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        headers = {"Content-Type": "application/json"}
    else:
        raise LLMSplitError("unknown protocol: %s" % p)
    # relays sitting behind a WAF answer the stdlib default User-Agent with a
    # bare 403, so the real segmentation call identifies itself too
    headers["User-Agent"] = USER_AGENT
    return url, payload, headers


def _request(endpoint: LLMEndpoint, system: str, user: str) -> str:
    """Build + send one chat request in the endpoint's native protocol."""
    url, payload, headers = _build_request(endpoint, system, user)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=endpoint.timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise LLMSplitError("HTTP %d from %s: %s" % (e.code, url, detail)) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LLMSplitError("request to %s failed: %s" % (url, e)) from e
    text = _content(body, endpoint.protocol)
    if not text:
        raise LLMSplitError("empty reply content (%s)" % endpoint.protocol)
    return text


# ---------------------------------------------------------------------------
# splitter
# ---------------------------------------------------------------------------

class ProtocolSplitter:
    """Three-protocol splitter driven by an :class:`LLMEndpoint`."""

    def __init__(self, endpoint: LLMEndpoint,
                 log: Callable[[str], None] | None = None) -> None:
        self.e = endpoint
        self._log = log or (lambda m: None)

    def choose(self, tasks: list[SplitTask],
               params: SegmentParams) -> dict[int, list[list[Word]]]:
        """Split all tasks into word-span pieces; partial failures degrade
        per-batch, total failure raises (the pipeline turns that into
        llm_note + full fallback)."""
        if not tasks:
            return {}
        summary = self._get_summary(tasks)
        out: dict[int, list[int]] = {}
        errors: list[str] = []
        for batch in self._batches(tasks):
            out.update(self._run_batch(batch, params, summary, errors))
        if errors:
            self._log("%d split batch(es) degraded to fallback: %s"
                      % (len(errors), "; ".join(errors)[:120]))
        if not out:
            raise LLMSplitError("all split batches failed: %s"
                                % "; ".join(errors)[:200])
        return out

    # ---------------------------------------------------------------- context

    def _get_summary(self, tasks: list[SplitTask]) -> str:
        """One cheap summary call for cross-chunk context (M6d). Failure is
        non-fatal: the split proceeds without it."""
        text = "\n".join(join_words(t.words) for t in tasks).strip()
        if not text:
            return ""
        self._log("生成全文摘要（断句上下文）...")
        try:
            content = _request(replace(self.e, temperature=0.5),
                               _SUMMARY_SYSTEM_PROMPT, text)
            self._log("摘要生成成功")
            return content
        except LLMSplitError as e:
            self._log("摘要生成失败（%s），无摘要继续断句" % e)
            return ""

    # ------------------------------------------------------------- degradation

    def _run_batch(self, batch: list[SplitTask], params: SegmentParams,
                   summary: str, errors: list[str]) -> dict[int, list[int]]:
        try:
            return self._choose_batch(batch, params, summary)
        except LLMSplitError as e:
            if len(batch) > 1:
                mid = len(batch) // 2
                self._log("批 %d 段失败（%s），对半拆分重试" % (len(batch), e))
                out = self._run_batch(batch[:mid], params, summary, errors)
                out.update(self._run_batch(batch[mid:], params, summary, errors))
                return out
            errors.append(str(e))
            return {}

    def _batches(self, tasks: list[SplitTask]) -> list[list[SplitTask]]:
        limit = self.e.budget_chars()
        batches, cur, size = [], [], 0
        for t in tasks:
            n = len(join_words(t.words))
            if cur and (len(cur) >= _MAX_TASKS_PER_REQUEST or size + n > limit):
                batches.append(cur)
                cur, size = [], 0
            cur.append(t)
            size += n
        if cur:
            batches.append(cur)
        return batches

    def _choose_batch(self, batch: list[SplitTask],
                      params: SegmentParams, summary: str) -> dict[int, list[list[Word]]]:
        if summary:
            user = ("【全文摘要】:\n%s\n\n【当前文本块】:\n"
                    "请断句以下 %d 段：\n" % (summary, len(batch)))
        else:
            user = "请断句以下 %d 段：\n" % len(batch)
        for t in batch:
            user += "\n[id=%d]\n%s\n" % (t.run_id, join_words(t.words))
        by_id = {t.run_id: t for t in batch}

        system = _SYSTEM_PROMPT.format(min_chars=_TARGET_MIN_CHARS,
                                       max_chars=params.max_chars)
        error_note = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                # BOTH the request and the parse/align sit inside the retry
                # loop: a retryable transport error must not escape early,
                # and a malformed/misaligned reply gets a corrective note
                content = _request(self.e, system + error_note, user)
                out: dict[int, list[list[Word]]] = {}
                for rid, lines in self._parse(content).items():
                    task = by_id.get(rid)
                    if task is None:
                        continue          # id the model invented/skipped
                    try:
                        out[rid] = _align_lines(lines, task.words)
                    except LLMSplitError as e:
                        self._log("段 id=%d 对齐失败：%s" % (rid, e))
                if not out:
                    raise LLMSplitError(
                        "reply had no alignable lines for this batch (mismatch)")
                return out
            except LLMSplitError as e:
                msg = str(e)
                if attempt >= _MAX_ATTEMPTS or not _is_retryable_error(msg):
                    raise
                low = msg.lower()
                if ("reply" in low or "json" in low or "mismatch" in low):
                    error_note = ("\n上次输出无法逐字对应原文（%s），"
                                  "请逐字保留原文、只调整断行位置，"
                                  "并严格只返回规定格式的 JSON。" % msg)
                else:
                    error_note = ""       # transport error: no parse note
                self._log("断句第 %d/%d 次尝试失败：%s"
                          % (attempt, _MAX_ATTEMPTS, msg))
                time.sleep(_RETRY_BACKOFF_S[min(attempt - 1,
                                                len(_RETRY_BACKOFF_S) - 1)])
        raise LLMSplitError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse(content: str) -> dict[int, list[str]]:
        results = _extract_json_object(content).get("results")
        if not isinstance(results, list):
            raise LLMSplitError("reply JSON has no results list")
        parsed: dict[int, list[str]] = {}
        for item in results:
            if (isinstance(item, dict) and isinstance(item.get("id"), int)
                    and isinstance(item.get("lines"), list)):
                lines = [s for s in item["lines"] if isinstance(s, str)]
                if lines:
                    parsed[item["id"]] = lines
        if not parsed:
            raise LLMSplitError("reply results list is empty")
        return parsed


class OpenAICompatibleSplitter(ProtocolSplitter):
    """M2-compatible constructor: takes an LLMConfig directly."""

    def __init__(self, cfg: LLMConfig, log=None) -> None:
        super().__init__(LLMEndpoint.from_legacy(cfg), log=log)


def resolve_endpoint(app_config: dict) -> LLMEndpoint:
    """Active endpoint from llm_active (+ provider/model meta), legacy fallback."""
    active = app_config.get("llm_active") or {}
    providers = {p.get("id"): p for p in app_config.get("llm_providers", [])}
    pid = active.get("provider")
    if pid in providers:
        p = providers[pid]
        models = {m.get("id"): m for m in p.get("models", [])}
        m = models.get(active.get("model"), {})
        return LLMEndpoint(
            protocol=p.get("protocol", "openai"),
            base_url=p.get("base_url", ""),
            api_key=p.get("api_key", ""),
            model=active.get("model", ""),
            effort=active.get("effort"),
            context_window=m.get("context_window"),
            max_output=m.get("max_output"),
        )
    from ..config import llm_config_from
    return LLMEndpoint.from_legacy(llm_config_from(app_config))
