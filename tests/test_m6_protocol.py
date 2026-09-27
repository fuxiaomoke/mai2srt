"""M6 tests: three-protocol request construction, endpoint resolution,
context-adaptive batching, and the UI emoji ban."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.config import LLMConfig
from mai2srt.segment.candidates import Candidate
from mai2srt.segment.llm import (
    LLMEndpoint,
    ProtocolSplitter,
    OpenAICompatibleSplitter,
    SplitTask,
    _build_request,
    resolve_endpoint,
)
from mai2srt.segment.rules import SegmentParams, join_words
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


def test_openai_path_joins_api_roots_correctly():
    """Bases that already name their API root take the leaf directly: the
    usual /v1, and Google's OpenAI-compat layer ending in /openai (an
    extra /v1 there 404s -- official docs and field reports agree)."""
    from mai2srt.segment.llm import _openai_path
    assert _openai_path("https://api.deepseek.com",
                        "chat/completions") == \
        "https://api.deepseek.com/v1/chat/completions"
    assert _openai_path("https://api.openai.com/v1/", "models") == \
        "https://api.openai.com/v1/models"
    g = "https://generativelanguage.googleapis.com/v1beta/openai"
    assert _openai_path(g, "chat/completions") == g + "/chat/completions"
    assert _openai_path(g, "models") == g + "/models"


def test_gemini_openai_compat_endpoint_shape():
    """The Gemini preset's openai format rides Google's compatibility
    layer: Bearer auth, no /v1 insertion, same builder as any openai call."""
    e = LLMEndpoint(
        protocol="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="k", model="gemini-3.8-flash", effort=None)
    url, payload, headers = _build_request(e, "S", "U")
    assert url == ("https://generativelanguage.googleapis.com"
                   "/v1beta/openai/chat/completions")
    assert headers["Authorization"] == "Bearer k"


def test_build_request_anthropic():
    # sonnet-4-5: extended-thinking-only generation -> budget mode
    e = LLMEndpoint(protocol="anthropic", base_url="https://api.anthropic.com",
                    api_key="ak", model="claude-sonnet-4-5", effort="high",
                    max_output=4096)
    url, payload, headers = _build_request(e, "SYS", "USER")
    assert url == "https://api.anthropic.com/v1/messages"
    assert payload["system"] == "SYS"
    assert payload["max_tokens"] == 4096
    # budget clamps under max_tokens MINUS the reply's room (anthropic
    # rejects budget >= max_tokens, and the reply needs its tokens too)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    # thinking enabled forbids temperature (must stay 1/unset) -> omitted
    assert "temperature" not in payload
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
    # lowercase enum per the docs' own REST examples; no temperature on the
    # 3 era (deprecated there, and <1.0 actively degrades these models)
    assert payload["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "medium"}
    assert "temperature" not in payload["generationConfig"]
    assert "Authorization" not in headers


def test_effort_off_removed_everywhere():
    assert LLMEndpoint(effort="off", protocol="anthropic").effort_kwargs() == {}
    assert LLMEndpoint(effort=None, protocol="gemini").effort_kwargs() == {}
    # gemini-3 cannot disable thinking: "off" sends nothing (dynamic stays)
    assert LLMEndpoint(effort="off", protocol="gemini",
                       model="gemini-3.8-flash").effort_kwargs() == {}


def test_effort_max_per_protocol():
    # openai: passes through verbatim (relays that reject it degrade safely)
    e = LLMEndpoint(protocol="openai", effort="max")
    assert e.effort_kwargs() == {"reasoning_effort": "max"}
    # gemini-3: thinkingLevel enum tops out at high; a nameless model
    # (relay alias, bare test endpoint) rides the 3-era default
    e = LLMEndpoint(protocol="gemini", effort="max")
    assert e.effort_kwargs()["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "high"}
    # anthropic budget mode: "max" wants 65536 but must leave the reply
    # its room inside the declared ceiling (2048 default reply budget)
    e = LLMEndpoint(protocol="anthropic", effort="max", max_output=8192)
    assert e.effort_kwargs()["thinking"]["budget_tokens"] == 8192 - 2048
    # unknown meta mirrors the 8192 request default
    e = LLMEndpoint(protocol="anthropic", effort="high", max_output=None)
    assert e.effort_kwargs()["thinking"]["budget_tokens"] == 8192 - 2048
    # a model with no room for thinking sends no thinking block at all
    e = LLMEndpoint(protocol="anthropic", effort="low", max_output=1500)
    assert e.effort_kwargs() == {}


# -------------------------------------------------- gemini thinking dispatch

def test_gemini_thinking_config_by_generation():
    """thinkingLevel era (Gemini 3+, lowercase values) vs thinkingBudget
    era (2.5, int with 0 = genuinely off) vs nothing (pre-2.5). Pro-tier
    3-series models only accept low/high -- medium folds up to high."""
    for effort, level in (("low", "low"), ("medium", "medium"),
                          ("high", "high"), ("max", "high")):
        e = LLMEndpoint(protocol="gemini", model="gemini-3.8-flash",
                        effort=effort)
        assert e.effort_kwargs()["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": level}, effort

    pro = LLMEndpoint(protocol="gemini", model="gemini-3.1-pro-preview",
                      effort="medium")
    assert pro.effort_kwargs()["generationConfig"]["thinkingConfig"] == {
        "thinkingLevel": "high"}          # 3-pro takes low/high only

    old = LLMEndpoint(protocol="gemini", model="gemini-2.5-flash",
                      effort="high")
    assert old.effort_kwargs()["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 24_576}
    off = LLMEndpoint(protocol="gemini", model="gemini-2.5-flash",
                      effort="off")
    assert off.effort_kwargs()["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0}              # the one era that can disable

    assert LLMEndpoint(protocol="gemini", model="gemini-2.0-flash",
                       effort="high").effort_kwargs() == {}
    assert LLMEndpoint(protocol="gemini", model="gemini-1.5-pro",
                       effort="high").effort_kwargs() == {}


def test_gemini_cap_leaves_room_for_thoughts():
    """Gemini 3 counts thought tokens against maxOutputTokens, so an
    explicit batch cap must ride ABOVE the reply budget; without thinking
    (or on the 2.5 budget era, whose thoughts count separately) it stays
    exactly what the batch asked for."""
    e = LLMEndpoint(protocol="gemini", model="gemini-3.8-flash",
                    effort="low", max_output=65_536)
    _, payload, _ = _build_request(e, "S", "U", 8_000)
    assert payload["generationConfig"]["maxOutputTokens"] == 8_000 + 16_384
    # ... clamped to the model's declared ceiling
    e2 = LLMEndpoint(protocol="gemini", model="gemini-3.8-flash",
                     effort="low", max_output=10_000)
    _, p2, _ = _build_request(e2, "S", "U", 8_000)
    assert p2["generationConfig"]["maxOutputTokens"] == 10_000
    # no thinking -> no allowance
    e3 = LLMEndpoint(protocol="gemini", model="gemini-3.8-flash",
                     effort=None, max_output=65_536)
    _, p3, _ = _build_request(e3, "S", "U", 8_000)
    assert p3["generationConfig"]["maxOutputTokens"] == 8_000
    # 2.5 budget era: thoughts bill outside maxOutputTokens
    e4 = LLMEndpoint(protocol="gemini", model="gemini-2.5-flash",
                     effort="high", max_output=65_536)
    _, p4, _ = _build_request(e4, "S", "U", 8_000)
    assert p4["generationConfig"]["maxOutputTokens"] == 8_000


def test_gemini_temperature_only_before_the_3_era():
    from mai2srt.segment.llm import _gemini_generation as gen
    assert gen("gemini-3.8-flash") == (3, 8)
    assert gen("gemini-2.5-pro") == (2, 5)
    assert gen("gemini-3-flash-preview") == (3, 0)
    assert gen("gpt-9") is None
    # 2.5 keeps temperature; 3.x and unknown names do not
    e25 = LLMEndpoint(protocol="gemini", model="gemini-2.5-flash",
                      temperature=0.0, effort=None)
    _, p25, _ = _build_request(e25, "S", "U")
    assert p25["generationConfig"]["temperature"] == 0.0
    e3 = LLMEndpoint(protocol="gemini", model="gemini-3.8-flash",
                     temperature=0.0, effort=None)
    _, p3, _ = _build_request(e3, "S", "U")
    assert "temperature" not in p3["generationConfig"]


# ------------------------------------------- anthropic thinking-mode dispatch

def test_anthropic_thinking_mode_by_generation():
    """Per-model thinking configurations from Anthropic's troubleshooting
    table: 4.7+/5.x/Fable/Mythos/Opus 4.5+/Sonnet 4.6+ take the effort
    parameter (and 4.7+ REJECT thinking.type=enabled with a 400); 3.7..4.5
    take budget_tokens; pre-3.7 takes nothing."""
    from mai2srt.segment.llm import _anthropic_thinking_mode as mode
    assert mode("claude-opus-4-6") == "effort"          # both, effort preferred
    assert mode("claude-opus-4-7") == "effort"          # rejects "enabled"
    assert mode("claude-opus-5") == "effort"
    assert mode("claude-opus-5-5") == "effort"
    assert mode("claude-sonnet-4-6") == "effort"
    assert mode("claude-fable-5") == "effort"
    assert mode("claude-mythos-preview") == "effort"
    assert mode("claude-sonnet-4-5-20250929") == "budget"
    assert mode("claude-opus-4-1-20250805") == "budget"
    assert mode("claude-haiku-4-5") == "budget"         # extended only
    assert mode("claude-3-7-sonnet-latest") == "budget"
    assert mode("claude-4-1") == "budget"
    assert mode("claude-3-5-sonnet-20240620") == "none"
    assert mode("claude-3-opus-20240229") == "none"
    # another vendor behind an anthropic-format relay: budget + runtime flip
    assert mode("deepseek-v4-pro") == "budget"


def test_anthropic_effort_mode_builds_output_config():
    """Adaptive-era models get output_config.effort, no thinking block, no
    temperature -- and a batch cap stays the plain reply ceiling (effort
    mode has no budget arithmetic to compose)."""
    e = LLMEndpoint(protocol="anthropic", base_url="https://relay.example",
                    api_key="k", model="claude-opus-4-8", effort="max",
                    max_output=64000, temperature=0.0)
    _, payload, _ = _build_request(e, "S", "U")
    assert payload["output_config"] == {"effort": "max"}
    assert "thinking" not in payload
    assert "temperature" not in payload          # deprecated on this era
    assert payload["max_tokens"] == 64000
    _, payload, _ = _build_request(e, "S", "U", 8000)
    assert payload["max_tokens"] == 8000


def test_anthropic_budget_rides_on_top_of_max_tokens():
    """Legacy thinking bills INSIDE max_tokens: the request must raise the
    ceiling by the budget so the reply keeps what batching sized it for,
    while the budget always stays strictly under the final max_tokens."""
    e = LLMEndpoint(protocol="anthropic", model="claude-opus-4-1-20250805",
                    effort="high", max_output=64000, temperature=0.0)
    _, payload, _ = _build_request(e, "S", "U", 8000)
    budget = payload["thinking"]["budget_tokens"]
    assert budget == min(16384, 64000 - 8000)     # reply room preserved
    assert payload["max_tokens"] == 8000 + budget
    assert budget < payload["max_tokens"]         # API rejects otherwise
    assert "temperature" not in payload
    # invariant across effort levels, caps and declared ceilings
    for effort in ("low", "medium", "high", "max"):
        for cap in (None, 4_000, 12_000, 63_000):
            for ceiling in (8_192, 64_000, None):
                ep = LLMEndpoint(protocol="anthropic",
                                 model="claude-4-1", effort=effort,
                                 max_output=ceiling)
                _, p, _ = _build_request(ep, "S", "U", cap)
                if "thinking" in p:
                    assert p["thinking"]["budget_tokens"] < p["max_tokens"], (
                        effort, cap, ceiling)
                    assert p["thinking"]["budget_tokens"] >= 1024
                else:
                    assert "temperature" in p    # no thinking -> temp allowed


def test_anthropic_temperature_sent_when_no_thinking():
    e = LLMEndpoint(protocol="anthropic", model="claude-3-5-sonnet",
                    effort="low", temperature=0.0)   # 3.5: mode none
    _, payload, _ = _build_request(e, "S", "U")
    assert "thinking" not in payload and "output_config" not in payload
    assert payload["temperature"] == 0.0
    # effort=None on a thinking model: no thinking kwargs either
    e2 = LLMEndpoint(protocol="anthropic", model="claude-opus-4-6",
                     effort=None, temperature=0.0)
    _, p2, _ = _build_request(e2, "S", "U")
    assert p2.get("temperature") == 0.0 and "output_config" not in p2


def test_thinking_mode_rejection_flip_directions():
    from mai2srt.segment.llm import _is_thinking_mode_rejection as flip
    rej = ('HTTP 400 from x: {"type":"error","message":"thinking.type.enabled '
           'is not supported"}')
    assert flip(rej, "budget") == "effort"       # 4.7+ rejects budgets
    assert flip(rej, "effort") == "none"         # already flipped: give up
    assert flip("HTTP 400 from x: output_config is not supported",
                "effort") == "budget"
    assert flip("HTTP 400 from x: invalid effort parameter", "effort") == "budget"
    assert flip("HTTP 400 from x: bad request", "effort") is None
    assert flip("HTTP 500 from x: thinking.type boom", "budget") is None


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


def test_output_budget_clamps_batching():
    """The reply is the batch's text re-emitted, so the generation ceiling
    binds as hard as the context window does."""
    # declared 8K output -> 4096-token budget -> ~6.8K chars, ~102 tasks
    small = LLMEndpoint(max_output=8_192)
    assert small.output_budget_tokens() == 4_096
    assert small.budget_chars() == int(4_096 / 0.6)
    assert small.budget_tasks() == 4_096 // 40
    # unknown metadata: the smallest published vendor ceiling (64K) applies,
    # so the 40K blast-radius cap is what actually binds
    unknown = LLMEndpoint()
    assert unknown.output_budget_tokens() == 32_000
    assert unknown.budget_chars() == 40_000
    assert unknown.budget_tasks() == 800
    # a huge declared output cannot lift the blast-radius cap either
    assert LLMEndpoint(max_output=524_288).budget_chars() == 40_000
    # context window still wins when it is the smaller of the two
    assert LLMEndpoint(context_window=8_000, max_output=524_288
                       ).budget_chars() == int(8_000 * 0.4 / 0.6)


def test_batching_is_char_bound_not_task_count():
    e = LLMEndpoint()                       # 40_000 chars, 800 tasks
    sp = ProtocolSplitter(e)

    def task(rid, n):
        words = [Word("あ", i * 0.2, i * 0.2 + 0.15) for i in range(n)]
        return SplitTask(rid, words, [Candidate(0, "punct_comma")])

    big = [task(i, 62) for i in range(300)]          # 18_600 chars in total
    assert len(sp._batches(big)) == 1                # used to be 15 at 20/request
    # ... while the scaffolding budget still guards thousands of tiny runs
    tiny = [task(i, 20) for i in range(1_000)]       # 20_000 chars, 1_000 ids
    batches = sp._batches(tiny)
    assert len(batches) == 2
    assert all(len(b) <= e.budget_tasks() for b in batches)


def test_packing_never_exceeds_the_generation_budget():
    """Every batch's ESTIMATED reply (text + per-task JSON scaffolding) must
    fit the generation budget -- a char-only rule silently over-packs hundreds
    of short runs, whose wrappers are then the bulk of the reply."""
    import mai2srt.segment.llm as llm
    for meta in (LLMEndpoint(max_output=8_192), LLMEndpoint(max_output=64_000),
                 LLMEndpoint()):
        sp = ProtocolSplitter(meta)
        for n in (20, 62):
            tasks = [SplitTask(rid, [Word("あ", i * 0.2, i * 0.2 + 0.15)
                                     for i in range(n)],
                               [Candidate(0, "punct_comma")])
                     for rid in range(700)]
            batches = sp._batches(tasks)
            assert len(batches) > 1                 # 43_400 chars: split up
            for b in batches:
                chars = sum(len(join_words(t.words)) for t in b)
                cost = int(chars * llm._CHARS_PER_TOKEN) \
                    + len(b) * llm._TASK_OVERHEAD_TOKENS
                assert cost <= meta.output_budget_tokens(), (n, len(b), cost)


def test_build_request_carries_output_cap_per_protocol():
    # each vendor names the field differently, and xAI/MiniMax deprecated
    # OpenAI's max_tokens in favour of max_completion_tokens
    o = LLMEndpoint(protocol="openai", base_url="https://api.deepseek.com",
                    api_key="k", model="m")
    _u, payload, _h = _build_request(o, "S", "U", 30_000)
    assert payload["max_completion_tokens"] == 30_000
    _u, payload, _h = _build_request(o, "S", "U")          # None: untouched
    assert "max_completion_tokens" not in payload

    a = LLMEndpoint(protocol="anthropic", base_url="https://a.example",
                    api_key="k", model="m", max_output=128_000)
    _u, payload, _h = _build_request(a, "S", "U", 30_000)
    assert payload["max_tokens"] == 30_000                 # the batch need wins
    _u, payload, _h = _build_request(a, "S", "U")
    assert payload["max_tokens"] == 128_000                # else the model meta

    g = LLMEndpoint(protocol="gemini", base_url="https://g.example",
                    api_key="k", model="m")
    _u, payload, _h = _build_request(g, "S", "U", 30_000)
    assert payload["generationConfig"]["maxOutputTokens"] == 30_000
    _u, payload, _h = _build_request(g, "S", "U")
    assert "maxOutputTokens" not in payload["generationConfig"]


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
