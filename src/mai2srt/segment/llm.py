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

Batching note: batches are packed by CHAR budget bounded by BOTH model
ceilings -- the context window (prompt side) and the generation budget (reply
side, from the model's declared max output with a conservative fallback), not
by a fixed task count. A batch big enough to outgrow a platform default also
carries an EXPLICIT generation cap; an endpoint that refuses that field is
remembered, and the batches shrink to a known-safe ceiling instead of failing.
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

#: batch guards -- the char ceiling is the blast-radius cap (one request that
#: drifts loses every run inside it), the rest is derived from the model.
_MAX_MARKED_CHARS_PER_REQUEST = 40_000

#: generation ceiling assumed when the endpoint's model metadata is unknown:
#: the SMALLEST published max-output among the current flagships and fast
#: tiers (Claude Haiku 4.5 = 64K; Gemini 3.8 Flash = 65,536; everything else
#: 128K-524K). Batching by the minimum keeps every vendor inside its budget.
_DEFAULT_MAX_OUTPUT_TOKENS = 64_000

#: share of that ceiling a batch may spend: thinking models bill their
#: chain-of-thought against the SAME generation budget (DeepSeek V4.1 thinks by
#: default, grok/GLM/Kimi/MiniMax cannot fully switch it off) and the reply
#: carries JSON scaffolding on top of the re-emitted text.
_OUTPUT_SAFETY = 0.5

#: JSON scaffolding charged per [id=N] task (braces, quotes, id, line breaks)
_TASK_OVERHEAD_TOKENS = 40

#: expected-output inflation over the raw text (quotes, ids, line breaks)
_JSON_OVERHEAD = 1.25

#: an explicit generation cap is only worth the field-name zoo once a batch
#: could actually outgrow a platform default; below this nothing is sent
_OUTPUT_CAP_FLOOR_TOKENS = 4_000

#: ceiling assumed after an endpoint REFUSES that field: the smallest platform
#: default documented by the vendors (DeepSeek, non-thinking mode = 8K)
_ASSUMED_DEFAULT_OUTPUT_TOKENS = 8_000

#: M6d: fixed target-range floor (user decision -- trans-jimaku-web's 10~50)
_TARGET_MIN_CHARS = 10

#: M6d: request retry chain (trans-jimaku-web parity)
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 2.0)
_RETRYABLE_HTTP = (408, 409, 425, 429, 500, 502, 503, 504)
_NON_RETRYABLE_HTTP = (400, 401, 403, 404)
_RETRYABLE_KEYWORDS = ("timeout", "timed out", "connect", "connection",
                       "network", "超时", "网络", "reply", "json", "mismatch")

#: field names an endpoint may name when it refuses our generation cap
_CAP_FIELD_RE = re.compile(
    r"max_completion_tokens|max_tokens|max_output_tokens|maxOutputTokens|"
    r"maximum.?tokens", re.I)

#: Claude version: an optional family word (claude-opus-4-6) then major and
#: minor. Family must be LETTERS or "claude-3-7-sonnet" would parse the
#: "3" as the family and the "7" as the major.
_CLAUDE_RE = re.compile(
    r"claude-(?:(fable|mythos|opus|sonnet|haiku)-)?(\d+)(?:[.-](\d+))?", re.I)

#: Gemini version: "gemini-3.8-flash" -> (3, 8), "gemini-2.5-pro" -> (2, 5).
_GEMINI_RE = re.compile(r"gemini-(\d+)(?:\.(\d+))?", re.I)

#: room left above an explicit maxOutputTokens for Gemini 3 thinking: the
#: cap counts THOUGHT tokens too (official docs), so the reply budget alone
#: would truncate the JSON mid-flight once thinking engages
_GEMINI_THINKING_ALLOWANCE_TOKENS = 16_384


def _gemini_generation(model: str) -> tuple[int, int] | None:
    m = _GEMINI_RE.search(model or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def _openai_path(base: str, leaf: str) -> str:
    """Join an OpenAI-compatible base URL with an endpoint leaf.

    A base that already names its API root takes the leaf directly: the
    usual trailing ``/v1``, and Google's OpenAI-compat layer whose root
    ends in ``/openai`` (inserting an extra ``/v1`` there 404s -- official
    docs and field reports agree). Anything else gets ``/v1`` inserted.
    """
    base = base.rstrip("/")
    if base.endswith("/v1") or base.endswith("/openai"):
        return "%s/%s" % (base, leaf)
    return "%s/v1/%s" % (base, leaf)


def _anthropic_thinking_mode(model: str) -> str:
    """How a model behind an Anthropic-format endpoint takes reasoning depth.

    'effort'  -- adaptive-thinking era: top-level ``output_config.effort``
                 (Claude 4.7+ and 5.x, Fable/Mythos, Opus 4.5+, Sonnet 4.6+).
                 These models REJECT ``thinking.type: "enabled"`` with a 400
                 (Claude 4.6 deprecates it but still accepts both).
    'budget'  -- legacy extended thinking via ``thinking.budget_tokens``
                 (Claude 3.7 through 4.5, Claude 4).
    'none'    -- pre-3.7 Claude: no thinking parameter at all.

    Source: platform.claude.com "Troubleshooting thinking" per-model table.
    A non-Claude name (another vendor behind an Anthropic-format relay)
    defaults to 'budget'; a wrong guess self-corrects at runtime via
    ``_is_thinking_mode_rejection``.
    """
    m = _CLAUDE_RE.search(model or "")
    if not m:
        # version-less family names exist in the wild ("claude-mythos-
        # preview"); Fable/Mythos are 5-era, so effort regardless
        fam_only = re.search(r"claude-(fable|mythos)\b", model or "", re.I)
        if fam_only:
            return "effort"
        return "budget"
    fam = (m.group(1) or "").lower()
    maj, minor = int(m.group(2)), int(m.group(3) or 0)
    if fam in ("fable", "mythos") or maj >= 5:
        return "effort"
    if maj == 4:
        # Opus 4.5+ / Sonnet 4.6+ take the effort parameter; Haiku 4.5
        # is extended-thinking-only, and Claude 4/4.1 take only budgets
        if (fam == "opus" and minor >= 5) or (fam == "sonnet" and minor >= 6):
            return "effort"
        if not fam and minor >= 7:      # family-less alias, e.g. "claude-4-7"
            return "effort"
        return "budget"
    if maj == 3:
        return "budget" if minor >= 7 else "none"
    return "none"

#: conservative token estimate for CJK-heavy subtitle text. The NAME is
#: historical: every formula in this module treats it as TOKENS PER CHAR
#: (``tokens = chars * _CHARS_PER_TOKEN``), i.e. 1 token ~ 1.67 characters.
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
    #: runtime kill-switch/override of the thinking parameters, set when a
    #: 4xx rejects the form we chose: anthropic flips between 'effort' |
    #: 'budget' | 'none' (_is_thinking_mode_rejection); gemini and openai
    #: only ever go 'none' (_is_gemini_thinking_rejection /
    #: _is_reasoning_effort_rejection). None = derive from the model name
    thinking_mode: str | None = None
    context_window: int | None = None     # tokens
    max_output: int | None = None         # tokens
    temperature: float = 0.0
    timeout_s: float = 120.0

    @classmethod
    def from_legacy(cls, cfg: LLMConfig) -> "LLMEndpoint":
        return cls(protocol="openai", base_url=cfg.base_url, api_key=cfg.api_key,
                   model=cfg.model, temperature=cfg.temperature,
                   timeout_s=cfg.timeout_s)

    def output_budget_tokens(self) -> int:
        """Generation tokens one batch may ask the model to produce."""
        declared = self.max_output or _DEFAULT_MAX_OUTPUT_TOKENS
        return int(declared * _OUTPUT_SAFETY)

    def input_budget_chars(self) -> int:
        """Char budget for a PROMPT: the context window, and the blast radius.

        Only the input side -- used where the reply is not the batch's text
        (the whole-job summary call).
        """
        budget = _MAX_MARKED_CHARS_PER_REQUEST
        if self.context_window and self.context_window > 0:
            adaptive = int(self.context_window * 0.4 / _CHARS_PER_TOKEN)
            budget = min(budget, max(2_000, adaptive))
        return budget

    def budget_chars(self) -> int:
        """Char budget per request, clamped by BOTH model ceilings.

        The prompt side is bounded by the context window; the reply side is the
        same text re-emitted as JSON, so the generation ceiling binds too --
        whichever is smaller wins.
        """
        out_chars = int(self.output_budget_tokens() / _CHARS_PER_TOKEN)
        return min(self.input_budget_chars(), max(2_000, out_chars))

    def budget_tasks(self) -> int:
        """How many [id=N] blocks fit the output budget's scaffolding alone.

        Bounds the pathological packing (thousands of tiny runs) that the char
        budget cannot see: each block costs its JSON wrapper even when its text
        is short.
        """
        return max(1, self.output_budget_tokens() // _TASK_OVERHEAD_TOKENS)

    def resolve_thinking_mode(self) -> str:
        """Resolved anthropic thinking mode (name-derived unless overridden)."""
        if self.protocol != "anthropic":
            return "budget"               # unused by the other protocols
        return self.thinking_mode or _anthropic_thinking_mode(self.model)

    def effort_kwargs(self, text_tokens: int | None = None) -> dict:
        """Protocol-native reasoning effort; empty when off/unsupported.

        ``text_tokens`` is the generation budget the caller wants to keep
        for the VISIBLE reply (None = unspecified). Anthropic's legacy
        thinking bills against the same ``max_tokens`` as the reply, so the
        budget is clamped to leave that room AND to stay strictly below
        ``max_tokens`` -- the API rejects ``budget_tokens >= max_tokens``
        and anything under the 1024-token floor.
        """
        if not self.effort:
            return {}
        if self.effort == "off" and self.protocol != "gemini":
            # gemini-2.5 can genuinely disable thinking (budget 0) and
            # handles "off" in its own branch below
            return {}
        if self.protocol == "openai":
            # dropped at runtime after a 4xx named the field
            if self.thinking_mode == "none":
                return {}
            # widely-adopted field; models that lack it ignore or reject --
            # a rejection surfaces as a normal LLM error and degrades safely
            return {"reasoning_effort": self.effort}
        if self.protocol == "anthropic":
            mode = self.resolve_thinking_mode()
            if mode == "none":
                return {}
            if mode == "effort":
                # adaptive-thinking era: the effort parameter IS the knob
                return {"output_config": {"effort": self.effort}}
            budgets = {"low": 1024, "medium": 4096, "high": 16384, "max": 65536}
            want = budgets.get(self.effort, 1024)
            ceiling = self.max_output or 8192
            # room the visible reply still needs inside max_tokens
            reply = text_tokens or 2048
            budget = min(want, ceiling - reply)
            if budget < 1024:
                # API floor is 1024: no room -> send no thinking block at all
                return {}
            return {"thinking": {"type": "enabled",
                                 "budget_tokens": budget}}
        if self.protocol == "gemini":
            # thinkingConfig killed at runtime by a 4xx flip
            if self.thinking_mode == "none":
                return {}
            gen = _gemini_generation(self.model)
            pro = "pro" in (self.model or "").lower()
            if gen is None or gen >= (3, 0):
                # thinkingLevel era. Values are lowercase per the docs'
                # own REST examples (and LiteLLM's production traffic).
                # Gemini 3 CANNOT fully disable thinking: "off" sends
                # nothing and the dynamic default stays on.
                if self.effort == "off":
                    return {}
                # 3-series Pro only accepts low/high (official per-model
                # table); everything else takes the full enum
                if pro and self.effort in ("medium", "high", "max"):
                    level = "high"
                else:
                    level = {"low": "low", "medium": "medium",
                             "high": "high", "max": "high"}.get(self.effort)
                if not level:
                    return {}
                return {"generationConfig": {"thinkingConfig": {
                    "thinkingLevel": level}}}
            if gen >= (2, 5):
                # budget era: an int token budget, 0 = really off
                budgets = {"off": 0, "low": 1024, "medium": 8_192,
                           "high": 24_576, "max": 24_576}
                budget = budgets.get(self.effort)
                if budget is None:
                    return {}
                return {"generationConfig": {"thinkingConfig": {
                    "thinkingBudget": budget}}}
            return {}          # pre-2.5: no thinking parameter at all
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


def _is_output_cap_rejection(err: str) -> bool:
    """True when a 4xx names our generation-cap field (unsupported parameter).

    Kept narrow on purpose: only a 4xx that explicitly mentions one of the
    field names counts, so a generic 400 still fails fast as before.
    """
    if not re.search(r"HTTP 4\d\d", err or ""):
        return False
    return bool(_CAP_FIELD_RE.search(err or ""))


def _is_thinking_mode_rejection(err: str, current: str) -> str | None:
    """Rewrite direction when a 4xx rejects our thinking parameter FORM.

    Returns the mode to switch to ('effort' / 'budget' / 'none'), or None
    when the error is not about the thinking parameter. Claude 4.7+ reject
    ``thinking.type: "enabled"`` with a 400; Opus 4.5 and earlier reject the
    adaptive/effort form; a model that rejects BOTH degrades to 'none' so a
    job never dies on the parameter shape alone.
    """
    if not re.search(r"HTTP 4\d\d", err or ""):
        return None
    low = err.lower()
    if "thinking.type" in low or "budget_tokens" in low:
        return "effort" if current != "effort" else "none"
    if "output_config" in low or "effort" in low:
        return "budget" if current != "budget" else "none"
    return None


def _is_gemini_thinking_rejection(err: str) -> bool:
    """True when a 4xx rejects our thinkingConfig: a level the model does
    not take (Pro only accepts low/high), a budget on a level-era model, or
    the other way round. One strike and thinking is off for the job."""
    if not re.search(r"HTTP 4\d\d", err or ""):
        return False
    return bool(re.search(
        r"thinkinglevel|thinking_budget|thinkingbudget|thinking_?config", err, re.I))


def _is_reasoning_effort_rejection(err: str) -> bool:
    """True when a 4xx names reasoning_effort: the openai-compatible field
    is widely adopted but not universal, and a model that rejects unknown
    fields would otherwise fail the whole batch."""
    if not re.search(r"HTTP 4\d\d", err or ""):
        return False
    return bool(re.search(r"reasoning[_ ]?effort", err, re.I))


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

def _build_request(endpoint: LLMEndpoint, system: str, user: str,
                   output_tokens: int | None = None) -> tuple[str, dict, dict]:
    """Pure request construction -> (url, payload, headers) per protocol.

    ``output_tokens`` asks the endpoint for an explicit generation ceiling.
    Field names differ per protocol (and xAI/MiniMax deprecated OpenAI's
    ``max_tokens`` in favour of ``max_completion_tokens``), so it is mapped
    here; ``None`` leaves the platform default alone.
    """
    p = endpoint.protocol
    if p == "openai":
        url = _openai_path(endpoint.base_url, "chat/completions")
        payload: dict = {
            "model": endpoint.model,
            "temperature": endpoint.temperature,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if output_tokens:
            payload["max_completion_tokens"] = output_tokens
        payload.update(endpoint.effort_kwargs())
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer %s" % endpoint.api_key}
    elif p == "anthropic":
        url = endpoint.base_url.rstrip("/") + "/v1/messages"
        # effort first: legacy thinking changes how max_tokens composes and
        # forbids temperature, so the payload depends on what it returns
        eff = endpoint.effort_kwargs(output_tokens)
        if eff.get("thinking") and output_tokens:
            # thinking bills against the SAME max_tokens as the reply: give
            # the budget ON TOP of the reply's ceiling. effort_kwargs
            # already shrank it so the sum stays inside the model's
            # declared maximum -- which also keeps budget < max_tokens.
            max_tokens = output_tokens + eff["thinking"]["budget_tokens"]
        else:
            max_tokens = output_tokens or endpoint.max_output or 8192
        payload = {
            "model": endpoint.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if not eff:
            # temperature only goes out when thinking is OFF: extended
            # thinking requires it 1/unset, and the adaptive-era models
            # deprecate it outright (the official SDKs dropped the field)
            payload["temperature"] = endpoint.temperature
        payload.update(eff)
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
        eff = endpoint.effort_kwargs(output_tokens)
        think = eff.get("generationConfig", {})
        gen: dict = {}
        gen.update(think)
        ggen = _gemini_generation(endpoint.model)
        # temperature is deprecated on Gemini 3+ and values < 1.0 actively
        # degrade them (LiteLLM force-resets it to 1.0); pre-3 keeps it.
        # An unparseable/absent name is treated as the 3 era.
        if ggen is not None and ggen < (3, 0):
            gen["temperature"] = endpoint.temperature
        if output_tokens:
            cap = output_tokens
            if "thinkingLevel" in think.get("thinkingConfig", {}):
                # maxOutputTokens counts THOUGHT tokens too on Gemini 3:
                # leave room above the reply budget or the JSON reply
                # truncates mid-flight once thinking engages
                cap = min(output_tokens + _GEMINI_THINKING_ALLOWANCE_TOKENS,
                          endpoint.max_output
                          or output_tokens + _GEMINI_THINKING_ALLOWANCE_TOKENS)
            gen["maxOutputTokens"] = cap
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


def _request(endpoint: LLMEndpoint, system: str, user: str,
             output_tokens: int | None = None) -> str:
    """Build + send one chat request in the endpoint's native protocol."""
    url, payload, headers = _build_request(endpoint, system, user, output_tokens)
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
        #: an explicit generation cap is sent for batches large enough to need
        #: one; an endpoint that refuses the field turns this off and shrinks
        #: the batches to a ceiling a platform default is known to cover
        self._send_cap = True
        self._cap_chars: int | None = None

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
        batches = self._batches(tasks)
        # a 300-run job can be several minutes of pure waiting: every batch
        # reports before it is sent, so the UI log is never blank in between
        self._log("断句：%d 段，分 %d 批发给 %s"
                  % (len(tasks), len(batches), self.e.model or "?"))
        for i, batch in enumerate(batches, 1):
            self._log("断句批次 %d/%d：%d 段，约 %d 字"
                      % (i, len(batches), len(batch),
                         sum(len(join_words(t.words)) for t in batch)))
            out.update(self._run_batch(batch, params, summary, errors))
        if errors:
            self._log("%d split batch(es) degraded to fallback: %s"
                      % (len(errors), "; ".join(errors)[:120]))
        if not out:
            raise LLMSplitError("all split batches failed: %s"
                                % "; ".join(errors)[:200])
        self._log("断句完成：%d/%d 段由大模型给出" % (len(out), len(tasks)))
        return out

    # ---------------------------------------------------------------- context

    def _get_summary(self, tasks: list[SplitTask]) -> str:
        """One cheap summary call for cross-chunk context (M6d). Failure is
        non-fatal: the split proceeds without it.

        The prompt side is the WHOLE job here (unlike the split calls, which
        are batched), so a long recording can exceed the model's context
        window. Trim to the input budget instead of letting the call fail:
        head and tail are kept (scene-setting and closure), the middle drops.
        """
        text = "\n".join(join_words(t.words) for t in tasks).strip()
        if not text:
            return ""
        budget = self.e.input_budget_chars()
        if len(text) > budget:
            head = int(budget * 0.6)
            tail = budget - head
            self._log("摘要文本 %d 字超出输入预算 %d 字，只送首尾各一段"
                      % (len(text), budget))
            text = text[:head] + "\n...\n" + text[-tail:]
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
        """Pack tasks by COST, not by a fixed task count.

        Charged against the generation budget per task: its text tokens
        (``_CHARS_PER_TOKEN``) PLUS its JSON scaffolding -- the char ceiling
        alone cannot see hundreds of tiny runs whose wrappers are the bulk of
        the reply. The input-side char budget (context window, blast radius)
        binds independently.
        """
        char_limit = self.e.budget_chars()
        if self._cap_chars is not None:
            char_limit = min(char_limit, self._cap_chars)
        token_limit = self.e.output_budget_tokens()
        max_tasks = self.e.budget_tasks()
        batches, cur, size, cost = [], [], 0, 0
        for t in tasks:
            n = len(join_words(t.words))
            # float accumulation on purpose: rounding each task down lets a
            # full batch drift over the budget (one token per task)
            step = n * _CHARS_PER_TOKEN + _TASK_OVERHEAD_TOKENS
            if cur and (len(cur) >= max_tasks or size + n > char_limit
                        or cost + step > token_limit):
                batches.append(cur)
                cur, size, cost = [], 0, 0
            cur.append(t)
            size += n
            cost += step
        if cur:
            batches.append(cur)
        return batches

    def _batch_cap(self, batch: list[SplitTask]) -> int | None:
        """Explicit generation ceiling for this batch (None = leave it alone).

        Only worth sending once the reply could outgrow a platform default;
        clamped to what the model itself declares when the metadata is known.
        """
        if not self._send_cap:
            return None
        text = sum(len(join_words(t.words)) for t in batch)
        need = int((text * _CHARS_PER_TOKEN
                    + len(batch) * _TASK_OVERHEAD_TOKENS) * _JSON_OVERHEAD)
        if need <= _OUTPUT_CAP_FLOOR_TOKENS:
            return None
        return min(self.e.max_output or _DEFAULT_MAX_OUTPUT_TOKENS, need)

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
        cap = self._batch_cap(batch)
        error_note = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                # BOTH the request and the parse/align sit inside the retry
                # loop: a retryable transport error must not escape early,
                # and a malformed/misaligned reply gets a corrective note
                content = _request(self.e, system + error_note, user,
                                   output_tokens=cap)
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
                # thinking-form rejection first (its message can also name
                # max_tokens, which would misfire the cap fallback below)
                if self.e.protocol == "anthropic":
                    flip = _is_thinking_mode_rejection(
                        msg, self.e.resolve_thinking_mode())
                    if flip:
                        # not a failure: the parameter FORM was wrong for
                        # this model (name heuristics missed, relay renamed
                        # it) -- remember the working form for the rest of
                        # the job and retry immediately
                        self.e.thinking_mode = flip
                        self._log("端点不接受思考参数形式（%s），改用 %s 重试"
                                  % (msg[:80],
                                     {"effort": "effort 参数",
                                      "budget": "thinking 预算",
                                      "none": "关闭思考"}[flip]))
                        continue
                elif (self.e.protocol == "gemini"
                        and _is_gemini_thinking_rejection(msg)):
                    # wrong level for this model (e.g. Pro takes low/high
                    # only) -- drop thinking for the rest of the job
                    self.e.thinking_mode = "none"
                    self._log("端点不接受思考参数（%s），已关闭思考重试"
                              % msg[:80])
                    continue
                elif (self.e.protocol == "openai"
                        and _is_reasoning_effort_rejection(msg)):
                    # not every openai-compatible backend takes the field
                    self.e.thinking_mode = "none"
                    self._log("端点不接受 reasoning_effort 参数（%s），"
                              "已关闭思考重试" % msg[:80])
                    continue
                if cap is not None and _is_output_cap_rejection(msg):
                    # the endpoint does not accept the field: drop it for the
                    # rest of the job and shrink later batches to a ceiling a
                    # platform default is known to cover. Immediate retry --
                    # this is not a failure, just an unsupported parameter.
                    self._send_cap = False
                    self._cap_chars = max(
                        2_000, int(_ASSUMED_DEFAULT_OUTPUT_TOKENS
                                   * _OUTPUT_SAFETY / _CHARS_PER_TOKEN))
                    cap = None
                    self._log("端点不接受生成上限参数（%s），已改为不发送并"
                              "把分批收紧到 %d 字" % (msg[:80], self._cap_chars))
                    continue
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
