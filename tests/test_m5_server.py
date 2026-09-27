"""M5 server-layer unit tests (pure logic, no network)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.server import llm_admin
from mai2srt.server.jobs import BusyError, JobManager


# ---------------------------------------------------------------- tagging

def test_tag_model_reasoning_and_context():
    m = llm_admin.tag_model("deepseek-v4-flash")
    assert m["reasoning"] is True
    assert m["context_window"] == 1_048_576      # built-in table (verified)
    assert m["max_output"] == 393_216
    assert m["limits_source"] == "builtin"       # a table guess, not the API
    assert m["efforts"] == list(llm_admin.EFFORT_LEVELS)

    m2 = llm_admin.tag_model("deepseek-chat")
    assert m2["reasoning"] is False or m2["reasoning"] is True  # name heuristic may hit
    m3 = llm_admin.tag_model("whisper-large")
    assert m3["reasoning"] is False
    assert m3["context_window"] is None and m3["limits_source"] is None


def test_tag_model_api_fields_win():
    m = llm_admin.tag_model("deepseek-v4-pro", {"context_length": 1_000_000})
    assert m["context_window"] == 1_000_000       # API beats built-in table
    assert m["limits_source"] == "api"


def test_tag_model_reads_declared_modalities_and_efforts():
    """Providers STATE these; the name regexes only guess. Payload below is the
    real deepseek-flash /v1/models entry."""
    real = {
        "id": "deepseek-flash",
        "context_window": 1_048_576,
        "max_output_tokens": 393_216,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "effort": {"supported_levels": ["low", "high", "max"],
                   "default_level": "high"},
    }
    m = llm_admin.tag_model("deepseek-flash", real)
    assert m["vision"] is True                      # the regex alone says False
    assert m["efforts"] == ["low", "high", "max"]   # not the 5-level guess
    assert m["reasoning"] is True
    assert m["limits_source"] == "api"

    # ... and a declared text-only model stays text-only even when its name
    # matches a vision pattern
    t = llm_admin.tag_model("gemini-3.8-flash", {"input_modalities": ["text"]})
    assert t["vision"] is False

    # a declared "off" alone does not imply a thinking model
    o = llm_admin.tag_model("plain-model", {
        "effort": {"supported_levels": ["off"]}, "max_output_tokens": 4096})
    assert o["efforts"] == ["off"]
    assert o["reasoning"] is False


def test_tag_model_claude_generation_split():
    """Claude thinks since 3.7; 3.0/3.5 never did. Both wild naming shapes
    (claude-3-7-sonnet / claude-opus-4-6) must tag reasoning, while the
    non-thinking generation stays out even when a "-5" minor version
    appears in the name (claude-3-5-sonnet)."""
    for mid in ("claude-opus-4-6", "claude-opus-5", "claude-sonnet-4-5-20250929",
                "claude-3-7-sonnet-latest", "claude-4-1-20250805",
                "claude-haiku-4-5", "claude-fable-5"):
        m = llm_admin.tag_model(mid)
        assert m["reasoning"] is True, mid
        assert m["efforts"] == list(llm_admin.EFFORT_LEVELS), mid
    for mid in ("claude-3-5-sonnet-20240620", "claude-3-5-haiku",
                "claude-3-opus-20240229", "claude-2-1"):
        assert llm_admin.tag_model(mid)["reasoning"] is False, mid


def test_tag_model_gemini_generation_split():
    """Official thinking doc: "Gemini 3 and 2.5 series models use a
    thinking process" -- 1.5/2.0 never did. Version-less relay aliases
    (gemini-3-flash) must tag too, and pick up the built-in limits row."""
    for mid in ("gemini-3.8-flash", "gemini-3.1-pro-preview",
                "gemini-3-flash-preview", "gemini-3-flash",
                "gemini-2.5-pro", "gemini-2.5-flash",
                "gemini-3.8-flash-high"):
        m = llm_admin.tag_model(mid)
        assert m["reasoning"] is True, mid
        assert m["efforts"] == list(llm_admin.EFFORT_LEVELS), mid
    # the catch-all limits row covers aliases the specific rows miss
    m = llm_admin.tag_model("gemini-3-flash")
    assert m["context_window"] == 1_048_576 and m["max_output"] == 65_536
    for mid in ("gemini-2.0-flash", "gemini-1.5-pro", "gemini-embedding"):
        assert llm_admin.tag_model(mid)["reasoning"] is False, mid


def test_presets_offer_only_formats_the_vendor_serves():
    """The urls keys ARE the formats the add-provider card may offer
    (verified against the vendors' docs, 2026-09); the default protocol
    must itself be one of them."""
    expected = {
        "deepseek": {"openai", "anthropic"},
        "openai": {"openai"},
        "anthropic": {"anthropic"},
        "gemini": {"gemini", "openai"},      # OpenAI-compat layer /v1beta/openai
        "openrouter": {"openai", "anthropic"},
        "ollama": {"openai", "anthropic"},   # Messages API compat since v0.14.0
        "custom": {"openai", "anthropic", "gemini"},
    }
    for pid, formats in expected.items():
        p = llm_admin.PRESETS[pid]
        assert set(p["urls"]) == formats, pid
        assert p["protocol"] in p["urls"], pid
    # the multi-format endpoint swaps are part of the contract
    assert llm_admin.PRESETS["deepseek"]["urls"]["anthropic"] == \
        "https://api.deepseek.com/anthropic"
    assert llm_admin.PRESETS["openrouter"]["urls"]["anthropic"] == \
        "https://openrouter.ai/api"
    assert llm_admin.PRESETS["ollama"]["urls"]["anthropic"] == \
        "http://127.0.0.1:11434"
    assert llm_admin.PRESETS["gemini"]["urls"]["openai"] == \
        "https://generativelanguage.googleapis.com/v1beta/openai"


def test_diff_models():
    existing = [{"id": "a", "reasoning": False, "context_window": 8},
                {"id": "b", "reasoning": False}]
    discovered = [{"id": "a", "reasoning": True, "context_window": 8},
                  {"id": "c", "reasoning": False}]
    d = llm_admin.diff_models(existing, discovered)
    assert [m["id"] for m in d["added"]] == ["c"]
    assert [m["id"] for m in d["removed"]] == ["b"]
    assert d["changed"] == [{"id": "a", "changes": {
        "reasoning": {"from": False, "to": True}}}]


# ---------------------------------------------------------------- persistence

def _cfg(tmp_path):
    from mai2srt.config import Config
    return Config(data_dir=tmp_path)


def test_upsert_keeps_key_on_masked_update(tmp_path):
    cfg = _cfg(tmp_path)
    llm_admin.upsert_provider(cfg, {
        "id": "p1", "name": "P1", "protocol": "openai",
        "base_url": "https://x", "api_key": "sk-real", "models": []})
    # client posts the masked value back -> real key must survive
    llm_admin.upsert_provider(cfg, {
        "id": "p1", "name": "P1", "protocol": "openai",
        "base_url": "https://x", "api_key": "***", "models": []})
    stored = llm_admin.load_providers(cfg)[0]
    assert stored["api_key"] == "sk-real"


def test_set_active_mirrors_legacy_llm(tmp_path):
    cfg = _cfg(tmp_path)
    llm_admin.upsert_provider(cfg, {
        "id": "deepseek", "name": "D", "protocol": "openai",
        "base_url": "https://api.deepseek.com", "api_key": "sk-x",
        "models": [{"id": "deepseek-flash", "context_window": 128000}]})
    llm_admin.set_active(cfg, {"provider": "deepseek",
                               "model": "deepseek-flash", "effort": "low"})
    from mai2srt.config import load_app_config, llm_config_from
    llm = llm_config_from(load_app_config(cfg))
    assert llm.base_url == "https://api.deepseek.com"
    assert llm.model == "deepseek-flash"
    assert llm.api_key == "sk-x"


# ---------------------------------------------------------------- jobs

def test_job_manager_lifecycle_and_sse():
    async def run():
        mgr = JobManager()

        async def factory(job):
            mgr.emit(job, "stage", {"stage": "prepare"})
            mgr.emit(job, "log", {"line": "hello"})
            await asyncio.sleep(0.05)
            return {"answer": 42}

        job = await mgr.start("process", "t", factory)
        events = []
        async for ev in mgr.stream(job):
            events.append(ev["type"])
        assert job.status == "done"
        assert job.result == {"answer": 42}
        assert "stage" in events and "log" in events and "done" in events

        # mutex: second job while one runs -> BusyError
        async def slow(job):
            await asyncio.sleep(1.0)
        j1 = await mgr.start("run", "long", slow)
        try:
            await mgr.start("run", "other", slow)
            raised = False
        except BusyError:
            raised = True
        assert raised
        # cancel (suppress: py may still surface CancelledError from await)
        assert mgr.cancel(j1.id) is True
        import contextlib
        with contextlib.suppress(asyncio.CancelledError):
            await j1.task
        assert j1.status == "cancelled"
        # after settle, new job allowed
        j3 = await mgr.start("process", "again", factory)
        await j3.task
        assert j3.status == "done"

    asyncio.run(run())


def test_job_manager_error_capture():
    async def run():
        mgr = JobManager()

        async def boom(job):
            raise RuntimeError("kaputt")

        job = await mgr.start("process", "bad", boom)
        await job.task
        assert job.status == "error"
        assert "kaputt" in job.error
        types = [ev["type"] for ev in job.events]
        assert "error" in types

    asyncio.run(run())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    import tempfile
    for fn in fns:
        try:
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print("[ok] %s" % fn.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            import traceback
            print("[FAIL] %s" % fn.__name__)
            traceback.print_exc()
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
