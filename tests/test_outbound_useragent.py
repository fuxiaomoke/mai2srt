"""Every outbound model request must carry a real User-Agent.

A relay station behind Cloudflare answers the standard-library default
(``Python-urllib/3.x``) with a bare ``403 Forbidden`` -- measured against one
such relay, deterministic across both of its keys, across streaming and
non-streaming, and gone the moment any User-Agent is set. In the UI that 403 is
indistinguishable from a wrong key or an unavailable model, so all three
outbound sites are pinned here: the provider test, model discovery, and the
real segmentation call.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.net import USER_AGENT                    # noqa: E402
from mai2srt.segment.llm import LLMEndpoint, _build_request  # noqa: E402
from mai2srt.server import llm_admin                  # noqa: E402


PROVIDER = {
    "id": "relay",
    "name": "Relay",
    "protocol": "openai",
    "base_url": "https://relay.example/v1",
    "api_key": "sk-test",
    "auth_style": "bearer",
    "models": [{"id": "m-one"}],
}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, body: dict | list) -> dict:
    seen: dict = {}

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        if req.data:
            seen["payload"] = json.loads(req.data.decode())
        return _Resp(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def _assert_real_agent(headers: dict) -> None:
    assert headers.get("user-agent") == USER_AGENT, headers
    assert "python-urllib" not in (headers.get("user-agent") or "").lower()


# ------------------------------------------------------------- the constant

def test_agent_is_not_the_stdlib_default():
    assert USER_AGENT.strip()
    assert "python-urllib" not in USER_AGENT.lower()


# --------------------------------------------- 1. the real segmentation call

def test_split_request_identifies_itself():
    for protocol in ("openai", "anthropic", "gemini"):
        endpoint = LLMEndpoint(protocol=protocol, base_url="https://relay.example",
                               api_key="sk-test", model="m-one", effort=None)
        _url, _payload, headers = _build_request(endpoint, "SYS", "USER")
        _assert_real_agent({k.lower(): v for k, v in headers.items()})


# ------------------------------------------------------- 2. the provider test

def test_provider_test_identifies_itself_per_protocol(monkeypatch):
    bodies = {
        "openai": {"choices": [{"message": {"content": "pong"}}]},
        "anthropic": {"content": [{"text": "pong"}]},
        "gemini": {"candidates": [{"content": {"parts": [{"text": "pong"}]}}]},
    }
    for protocol, body in bodies.items():
        seen = _capture(monkeypatch, body)
        llm_admin.test_provider(dict(PROVIDER, protocol=protocol), "m-one")
        _assert_real_agent(seen["headers"])


# --------------------------------------------------------- 3. model discovery

def test_discovery_identifies_itself(monkeypatch):
    seen = _capture(monkeypatch, {"data": [{"id": "m-one"}]})
    llm_admin.discover_models(dict(PROVIDER))
    _assert_real_agent(seen["headers"])
    assert seen["headers"].get("authorization") == "Bearer sk-test"


def test_discovery_keeps_an_explicit_agent(monkeypatch):
    """The default is a fallback: a caller that sets its own header wins."""
    import mai2srt.server.llm_admin as mod

    seen = _capture(monkeypatch, {"data": []})
    mod._http_json("https://relay.example/v1/models", {"User-Agent": "custom/9"})
    assert seen["headers"]["user-agent"] == "custom/9"
