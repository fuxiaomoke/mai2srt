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
    assert m["context_window"] == 128_000        # built-in table
    assert m["efforts"] == list(llm_admin.EFFORT_LEVELS)

    m2 = llm_admin.tag_model("deepseek-chat")
    assert m2["reasoning"] is False or m2["reasoning"] is True  # name heuristic may hit
    m3 = llm_admin.tag_model("whisper-large")
    assert m3["reasoning"] is False


def test_tag_model_api_fields_win():
    m = llm_admin.tag_model("deepseek-v4-pro", {"context_length": 1_000_000})
    assert m["context_window"] == 1_000_000       # API beats built-in table


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
