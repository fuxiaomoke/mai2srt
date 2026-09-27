"""Which model does a provider's "test" button actually ping?

The card-level button names no model, so the backend falls back to the first
listed model; the per-row button names one explicitly. Both paths must reach
the outgoing request payload unchanged -- the button tooltip promises exactly
that target, so a silent redirect to some other model would make the UI lie.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from mai2srt.config import Config  # noqa: E402
from mai2srt.server import llm_admin  # noqa: E402
from mai2srt.server.app import create_app  # noqa: E402


PROVIDER = {
    "id": "p1",
    "name": "Provider One",
    "protocol": "openai",
    "base_url": "https://api.example/v1",
    "api_key": "sk-test",
    "models": [{"id": "first-model"}, {"id": "second-model"}],
}


class _Resp(io.BytesIO):
    """urlopen() result: enough of the protocol for test_provider()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, body: dict) -> dict:
    """Stub the network; hand back what the request would have carried."""
    seen: dict = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data.decode())
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def _app(tmp_path):
    """(client, cfg) sharing one data dir, so seeding is visible to routes."""
    cfg = Config(data_dir=tmp_path)
    return TestClient(create_app(cfg)), cfg


_CHAT_OK = {"choices": [{"message": {"content": "pong"}}]}


# ------------------------------------------------------------- target choice

def test_named_model_is_the_one_pinged(monkeypatch):
    seen = _capture(monkeypatch, _CHAT_OK)
    out = llm_admin.test_provider(dict(PROVIDER), "second-model")
    assert out["model"] == "second-model"
    assert seen["payload"]["model"] == "second-model"
    # a ping carries no generation cap: it must not depend on cap-field
    # support (jobs only send one once a batch could outgrow a default)
    assert "max_tokens" not in seen["payload"]
    assert "max_completion_tokens" not in seen["payload"]


def test_unnamed_model_falls_back_to_the_first_listed(monkeypatch):
    seen = _capture(monkeypatch, _CHAT_OK)
    llm_admin.test_provider(dict(PROVIDER))
    assert seen["payload"]["model"] == "first-model"


def test_empty_model_list_is_an_error(monkeypatch):
    _capture(monkeypatch, _CHAT_OK)
    try:
        llm_admin.test_provider(dict(PROVIDER, models=[]))
    except ValueError as e:
        assert "no model" in str(e)
    else:
        raise AssertionError("an empty model list must not look like a pass")


# ------------------------------------------------------------------- routing

def test_route_forwards_the_model_query_param(tmp_path, monkeypatch):
    c, cfg = _app(tmp_path)
    llm_admin.upsert_provider(cfg, PROVIDER)
    calls: list[tuple] = []

    def fake(p, m=None, effort=None, timeout=60.0):
        calls.append((p["id"], m, effort))
        return {"ok": True, "model": m or "", "sample": ""}

    monkeypatch.setattr(llm_admin, "test_provider", fake)
    r = c.post("/api/llm/providers/p1/test", params={"model": "second-model"})
    assert r.status_code == 200
    assert r.json()["model"] == "second-model"
    assert calls == [("p1", "second-model", None)]


def test_route_forwards_the_effort_query_param(tmp_path, monkeypatch):
    """The active provider's test carries the active effort, so the ping
    exercises the request shape a job would send (thinking included)."""
    c, cfg = _app(tmp_path)
    llm_admin.upsert_provider(cfg, PROVIDER)
    calls: list[tuple] = []

    def fake(p, m=None, effort=None, timeout=60.0):
        calls.append(effort)
        return {"ok": True, "model": m or "", "sample": ""}

    monkeypatch.setattr(llm_admin, "test_provider", fake)
    assert c.post("/api/llm/providers/p1/test",
                  params={"model": "first-model", "effort": "max"}
                  ).status_code == 200
    assert calls == ["max"]
    # an invalid level is rejected before any network I/O
    assert c.post("/api/llm/providers/p1/test",
                  params={"effort": "sideways"}).status_code == 400


def test_route_without_model_leaves_the_choice_to_the_backend(tmp_path, monkeypatch):
    c, cfg = _app(tmp_path)
    llm_admin.upsert_provider(cfg, PROVIDER)
    calls: list[tuple] = []

    def fake(p, m=None, effort=None, timeout=60.0):
        calls.append((p["id"], m))
        return {"ok": True, "model": m or "", "sample": ""}

    monkeypatch.setattr(llm_admin, "test_provider", fake)
    r = c.post("/api/llm/providers/p1/test")
    assert r.status_code == 200
    assert calls == [("p1", None)]        # None -> models[0] inside test_provider


def test_unknown_provider_is_404(tmp_path, monkeypatch):
    c, _ = _app(tmp_path)
    monkeypatch.setattr(llm_admin, "test_provider",
                        lambda p, m=None, effort=None, timeout=60.0: {"ok": True})
    assert c.post("/api/llm/providers/nope/test").status_code == 404


# ------------------------------------------------ thinking reaches the relay

_ANTHROPIC_OK = {"content": [{"type": "text", "text": "pong"}]}


def test_ping_carries_the_thinking_parameter(monkeypatch):
    """The test ping goes through the same builder as real jobs: with an
    effort set, an anthropic-format relay must actually SEE the thinking
    parameter (output_config on adaptive-era models, budget block on
    legacy ones) -- before, the ping was a bare payload and the relay's
    thinking column stayed blank no matter what the UI said."""
    seen = _capture(monkeypatch, _ANTHROPIC_OK)
    provider = dict(PROVIDER, protocol="anthropic",
                    base_url="https://relay.example/anthropic",
                    models=[{"id": "claude-opus-4-8", "max_output": 64000}])
    llm_admin.test_provider(provider, "claude-opus-4-8", "max")
    assert seen["payload"]["output_config"] == {"effort": "max"}
    assert "temperature" not in seen["payload"]

    seen = _capture(monkeypatch, _ANTHROPIC_OK)
    provider = dict(PROVIDER, protocol="anthropic",
                    base_url="https://relay.example/anthropic",
                    models=[{"id": "claude-opus-4-1", "max_output": 64000}])
    llm_admin.test_provider(provider, "claude-opus-4-1", "low")
    thinking = seen["payload"]["thinking"]
    assert thinking["type"] == "enabled" and thinking["budget_tokens"] >= 1024
    assert thinking["budget_tokens"] < seen["payload"]["max_tokens"]
