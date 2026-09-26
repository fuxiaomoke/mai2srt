"""M6 tests: three-protocol request construction, endpoint resolution,
context-adaptive batching, and the UI emoji ban."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.config import LLMConfig
from mai2srt.segment.llm import (
    LLMEndpoint,
    ProtocolSplitter,
    OpenAICompatibleSplitter,
    _build_request,
    resolve_endpoint,
)
from mai2srt.segment.rules import SegmentParams
from mai2srt.transcribe.parser import Word


# --------------------------------------------------------- request building

def test_build_request_openai():
    e = LLMEndpoint(protocol="openai", base_url="https://api.deepseek.com",
                    api_key="sk-x", model="deepseek-flash", effort="low")
    url, payload, headers = _build_request(e, "SYS", "USER")
    assert url == "https://api.deepseek.com/v1/chat/completions"
    assert payload["model"] == "deepseek-flash"
    assert payload["messages"][0] == {"role": "system", "content": "SYS"}
    assert payload["reasoning_effort"] == "low"
    assert headers["Authorization"] == "Bearer sk-x"


def test_build_request_openai_relay_with_v1():
    e = LLMEndpoint(protocol="openai", base_url="https://relay.example/v1",
                    api_key="k", model="m", effort=None)
    url, payload, _ = _build_request(e, "S", "U")
    assert url == "https://relay.example/v1/chat/completions"
    assert "reasoning_effort" not in payload          # no effort -> no field


def test_build_request_anthropic():
    e = LLMEndpoint(protocol="anthropic", base_url="https://api.anthropic.com",
                    api_key="ak", model="claude-sonnet-4-5", effort="high",
                    max_output=4096)
    url, payload, headers = _build_request(e, "SYS", "USER")
    assert url == "https://api.anthropic.com/v1/messages"
    assert payload["system"] == "SYS"
    assert payload["max_tokens"] == 4096
    # budget clamps strictly under max_tokens (anthropic rejects otherwise)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 3072}
    assert headers["x-api-key"] == "ak"
    # dual auth: Bearer is Anthropic's primary header, x-api-key the
    # legacy fallback -- both sent so Bearer-only gateways work too
    assert headers["Authorization"] == "Bearer ak"
    assert headers["anthropic-version"] == "2023-06-01"


def test_build_request_gemini():
    e = LLMEndpoint(protocol="gemini",
                    base_url="https://generativelanguage.googleapis.com",
                    api_key="gk", model="gemini-3.8-flash", effort="medium")
    url, payload, headers = _build_request(e, "SYS", "USER")
    assert url.startswith(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent")
    assert "key=gk" in url
    assert payload["systemInstruction"]["parts"][0]["text"] == "SYS"
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
    assert "Authorization" not in headers


def test_effort_off_removed_everywhere():
    assert LLMEndpoint(effort="off", protocol="anthropic").effort_kwargs() == {}
    assert LLMEndpoint(effort=None, protocol="gemini").effort_kwargs() == {}


def test_effort_max_per_protocol():
    # openai: passes through verbatim (relays that reject it degrade safely)
    e = LLMEndpoint(protocol="openai", effort="max")
    assert e.effort_kwargs() == {"reasoning_effort": "max"}
    # gemini: thinkingLevel tops out at HIGH
    e = LLMEndpoint(protocol="gemini", effort="max")
    assert e.effort_kwargs()["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "HIGH"}
    # anthropic: "max" wants 65536 but clamps under the model's max_tokens
    e = LLMEndpoint(protocol="anthropic", effort="max", max_output=8192)
    assert e.effort_kwargs()["thinking"]["budget_tokens"] == 8192 - 1024
    # unknown meta mirrors the 8192 request default
    e = LLMEndpoint(protocol="anthropic", effort="high", max_output=None)
    assert e.effort_kwargs()["thinking"]["budget_tokens"] == 8192 - 1024
    # a model with no room for thinking sends no thinking block at all
    e = LLMEndpoint(protocol="anthropic", effort="low", max_output=1500)
    assert e.effort_kwargs() == {}


# --------------------------------------------------------- endpoint resolve

def test_resolve_endpoint_prefers_active(tmp_path=None):
    from mai2srt.server import llm_admin
    cfg_data = {
        "llm_providers": [{
            "id": "relay", "name": "R", "protocol": "anthropic",
            "base_url": "https://r.example", "api_key": "rk",
            "models": [{"id": "claude-x", "context_window": 200000}],
        }],
        "llm_active": {"provider": "relay", "model": "claude-x", "effort": "high"},
        "llm": {"base_url": "https://legacy", "api_key": "lk", "model": "old"},
    }
    e = resolve_endpoint(cfg_data)
    assert e.protocol == "anthropic"
    assert e.model == "claude-x" and e.api_key == "rk"
    assert e.context_window == 200000 and e.effort == "high"


def test_resolve_endpoint_legacy_fallback():
    e = resolve_endpoint({"llm": {"base_url": "https://api.deepseek.com",
                                  "api_key": "sk-l", "model": "m"}})
    assert e.protocol == "openai" and e.model == "m" and e.api_key == "sk-l"


def test_context_budget_clamp():
    small = LLMEndpoint(context_window=8_000)
    big = LLMEndpoint(context_window=1_000_000)
    none_meta = LLMEndpoint()
    assert small.budget_chars() == max(2_000, int(8_000 * 0.4 / 0.6))
    assert big.budget_chars() == 40_000
    assert none_meta.budget_chars() == 40_000


def test_batching_respects_budget():
    from mai2srt.segment.llm import SplitTask
    from mai2srt.segment.candidates import Candidate
    e = LLMEndpoint(context_window=8_000)   # budget -> 5333 chars
    sp = ProtocolSplitter(e)
    tasks = []
    for rid in range(6):
        words = [Word("あ", i * 0.3, i * 0.3 + 0.2) for i in range(1500)]  # ~1500 chars each
        cands = [Candidate(i * 50, "punct_comma") for i in range(1, 28)]
        tasks.append(SplitTask(rid, words, cands))
    batches = sp._batches(tasks)
    assert len(batches) > 1
    for b in batches:
        assert sum(len(t.marked_text()) for t in b) <= e.budget_chars() + 1500


# --------------------------------------------------------- legacy interface

def test_openai_compatible_splitter_still_works_as_type():
    s = OpenAICompatibleSplitter(LLMConfig(api_key="k", model="m"))
    assert isinstance(s, ProtocolSplitter)
    assert s.e.protocol == "openai"


# --------------------------------------------------------- emoji ban (UI)

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0001F900-\U0001F9FF"
    "\u2764\u2705\u274C\u2728]")


def test_ui_source_contains_no_emoji():
    app_src = Path(__file__).resolve().parents[1] / "app" / "src"
    hits = []
    for f in app_src.rglob("*"):
        if f.suffix in (".ts", ".tsx", ".css"):
            text = f.read_text(encoding="utf-8")
            for m in _EMOJI_RE.finditer(text):
                hits.append((f.name, m.start(), m.group()))
    assert hits == [], "emoji found in UI source: %r" % hits[:5]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    import tempfile
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
