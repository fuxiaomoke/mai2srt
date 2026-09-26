"""LLM provider administration (PLAN-UI 4): CRUD, discovery, capability
tagging and connectivity tests across the three protocols.

Model of dsh-model-sync's adapter-registry + normalizer + diff flow, scaled
to mai2srt: 3 protocols, 7 presets, heuristic capability tagging, discovered
diffs that are returned for confirmation (never silently saved).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from ..config import Config, load_app_config, save_app_config
from ..net import USER_AGENT

PROTOCOLS = ("openai", "anthropic", "gemini")
AUTH_STYLES = ("bearer", "x-api-key", "query-key", "none")

#: one-click presets (id -> display info). `alt_base_urls` maps a
#: NON-default protocol to its default endpoint so the add-provider card
#: can retarget the URL on API-format switch; verified endpoints:
#: OpenRouter Anthropic-format base https://openrouter.ai/api (SDK then
#: appends /v1/messages), DeepSeek Anthropic-format base
#: https://api.deepseek.com/anthropic.
PRESETS = {
    "deepseek":   {"name": "DeepSeek 官方", "protocol": "openai",
                   "base_url": "https://api.deepseek.com", "auth_style": "bearer",
                   "alt_base_urls": {"anthropic": "https://api.deepseek.com/anthropic"}},
    "openai":     {"name": "OpenAI", "protocol": "openai",
                   "base_url": "https://api.openai.com/v1", "auth_style": "bearer"},
    "anthropic":  {"name": "Anthropic Claude", "protocol": "anthropic",
                   "base_url": "https://api.anthropic.com", "auth_style": "x-api-key"},
    "gemini":     {"name": "Google Gemini", "protocol": "gemini",
                   "base_url": "https://generativelanguage.googleapis.com",
                   "auth_style": "query-key"},
    "openrouter": {"name": "OpenRouter", "protocol": "openai",
                   "base_url": "https://openrouter.ai/api/v1", "auth_style": "bearer",
                   "alt_base_urls": {"anthropic": "https://openrouter.ai/api"}},
    "ollama":     {"name": "Ollama (local)", "protocol": "openai",
                   "base_url": "http://127.0.0.1:11434/v1", "auth_style": "none"},
    "custom":     {"name": "自定义 OpenAI 兼容", "label": "自定义", "protocol": "openai",
                   "base_url": "", "auth_style": "bearer"},
}

#: built-in context/output hints for common model families (tokens)
_BUILTIN_META: list[tuple[str, int, int]] = [
    (r"deepseek.*(v4|flash)", 128_000, 16_384),
    (r"deepseek.*(pro|reasoner|r1)", 128_000, 64_000),
    (r"gpt-5\.4", 400_000, 128_000),
    (r"gpt-4\.1", 1_000_000, 32_768),
    (r"gpt-4o", 128_000, 16_384),
    (r"o[34](-mini)?", 200_000, 100_000),
    (r"claude.*(-4-6|4-6|opus-4-6)", 200_000, 64_000),
    (r"claude.*sonnet-4-5", 200_000, 64_000),
    (r"claude.*haiku", 200_000, 32_768),
    (r"gemini-3\.[0-9]+-flash", 1_000_000, 65_536),
    (r"gemini.*pro", 1_000_000, 65_536),
    (r"glm-5\.[0-9]+", 128_000, 32_768),
    (r"qwen3", 131_072, 8_192),
]

_REASONING_RE = re.compile(
    r"r1|reasoner|o[134](-mini|-pro)?(\b|-)|think|max|ultra|deepseek-v4|"
    r"gpt-5|deepseek-flash|deepseek-pro", re.I)
_VISION_RE = re.compile(r"vl|4o(\b|-)|gemini|claude|glm-4v|qwen-vl|vision", re.I)

EFFORT_LEVELS = ("off", "low", "medium", "high", "max")


def tag_model(model_id: str, api_meta: dict | None = None) -> dict:
    """Heuristic capability tagging; API-provided fields win when present."""
    meta = {
        "id": model_id,
        "context_window": None,
        "max_output": None,
        "reasoning": bool(_REASONING_RE.search(model_id)),
        "vision": bool(_VISION_RE.search(model_id)),
        "efforts": None,
        "source": "discovered",
    }
    for pat, ctx, out in _BUILTIN_META:
        if re.search(pat, model_id, re.I):
            meta["context_window"] = ctx
            meta["max_output"] = out
            break
    if api_meta:
        for k_src, k_dst in (("context_length", "context_window"),
                             ("context_window", "context_window"),
                             ("max_output_tokens", "max_output")):
            v = api_meta.get(k_src)
            if isinstance(v, int) and v > 0:
                meta[k_dst] = v
    if meta["reasoning"]:
        meta["efforts"] = list(EFFORT_LEVELS)
    return meta


# ---------------------------------------------------------------------------
# config persistence
# ---------------------------------------------------------------------------

def load_providers(cfg: Config) -> list[dict]:
    data = load_app_config(cfg)
    return [dict(p) for p in data.get("llm_providers", [])]


def save_providers(cfg: Config, providers: list[dict]) -> None:
    data = load_app_config(cfg)
    data["llm_providers"] = providers
    save_app_config(cfg, data)


def provider_public(p: dict) -> dict:
    """Provider without the api_key secret."""
    out = dict(p)
    if out.get("api_key"):
        out["api_key"] = "***"
    return out


def upsert_provider(cfg: Config, provider: dict) -> dict:
    providers = load_providers(cfg)
    pid = provider.get("id") or provider["name"].lower().replace(" ", "-")
    provider = dict(provider)
    provider["id"] = pid
    # keep the stored key when the client posts the masked value back
    for i, existing in enumerate(providers):
        if existing["id"] == pid:
            if not provider.get("api_key") or provider["api_key"] == "***":
                provider["api_key"] = existing.get("api_key", "")
            providers[i] = provider
            break
    else:
        if not provider.get("api_key"):
            provider["api_key"] = ""
        providers.append(provider)
    save_providers(cfg, providers)
    return provider


def delete_provider(cfg: Config, provider_id: str) -> bool:
    providers = load_providers(cfg)
    rest = [p for p in providers if p.get("id") != provider_id]
    if len(rest) == len(providers):
        return False
    save_providers(cfg, rest)
    return True


def get_active(cfg: Config) -> dict:
    return load_app_config(cfg).get("llm_active") or {}


def set_active(cfg: Config, active: dict) -> dict:
    data = load_app_config(cfg)
    data["llm_active"] = active
    # mirror into the legacy single-endpoint shape so the runner/CLI keep
    # working until the splitter is protocol-aware (M6)
    providers = {p.get("id"): p for p in data.get("llm_providers", [])}
    pid = active.get("provider")
    if pid in providers and providers[pid].get("protocol") == "openai":
        p = providers[pid]
        model = next((m for m in p.get("models", [])
                      if m["id"] == active.get("model")), None)
        data["llm"] = {
            "base_url": p.get("base_url", ""),
            "api_key": p.get("api_key", ""),
            "model": active.get("model", ""),
            "temperature": 0.0,
        }
        if model and model.get("context_window"):
            data["llm"]["context_window"] = model["context_window"]
    save_app_config(cfg, data)
    return active


# ---------------------------------------------------------------------------
# discovery + test (network)
# ---------------------------------------------------------------------------

def _http_json(url: str, headers: dict | None = None, timeout: float = 30.0):
    # a WAF-fronted relay answers the stdlib default User-Agent with a bare 403;
    # an explicitly passed User-Agent still wins over this one
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_models(provider: dict) -> list[dict]:
    """Fetch the provider's model list; returns tagged model dicts."""
    protocol = provider.get("protocol", "openai")
    base = provider.get("base_url", "").rstrip("/")
    key = provider.get("api_key", "")
    auth = provider.get("auth_style", "bearer")

    if protocol == "openai":
        url = base + "/models" if base.endswith("/v1") else base + "/v1/models"
        hdrs = {"Authorization": "Bearer %s" % key} if (key and auth == "bearer") else {}
        data = _http_json(url, hdrs)
        raw = data.get("data") if isinstance(data, dict) else data
        out = []
        for m in raw or []:
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if not mid:
                continue
            out.append(tag_model(mid, m if isinstance(m, dict) else None))
        return out
    if protocol == "anthropic":
        url = base + "/v1/models?limit=1000"
        # dual auth headers: Bearer for gateway-style Anthropic-format
        # endpoints (OpenRouter), x-api-key as Anthropic's legacy fallback
        hdrs = {"Authorization": "Bearer %s" % key, "x-api-key": key,
                "anthropic-version": "2023-06-01"}
        data = _http_json(url, hdrs)
        out = []
        for m in data.get("data", []):
            mid = m.get("id")
            if mid:
                out.append(tag_model(mid))
        return out
    if protocol == "gemini":
        url = base + "/v1beta/models?key=%s&pageSize=1000" % key
        data = _http_json(url)
        out = []
        for m in data.get("models", []):
            mid = (m.get("name") or "").removeprefix("models/")
            if mid and "generateContent" in (m.get("supportedGenerationMethods") or []):
                meta = tag_model(mid)
                for src, dst in (("inputTokenLimit", "context_window"),
                                 ("outputTokenLimit", "max_output")):
                    v = m.get(src)
                    if isinstance(v, int):
                        meta[dst] = v
                out.append(meta)
        return out
    raise ValueError("unknown protocol: %s" % protocol)


def diff_models(existing: list[dict], discovered: list[dict]) -> dict:
    """dsh-model-sync style diff: added / removed / changed."""
    old = {m["id"]: m for m in existing or []}
    new = {m["id"]: m for m in discovered or []}
    added = [m for mid, m in new.items() if mid not in old]
    removed = [old[mid] for mid in old if mid not in new]
    changed = []
    for mid, m in new.items():
        if mid in old:
            keys = ("context_window", "max_output", "reasoning", "vision")
            delta = {k: {"from": old[mid].get(k), "to": m.get(k)}
                     for k in keys if old[mid].get(k) != m.get(k)}
            if delta:
                changed.append({"id": mid, "changes": delta})
    return {"added": added, "removed": removed, "changed": changed}


def test_provider(provider: dict, model_id: str | None = None,
                  timeout: float = 60.0) -> dict:
    """Minimal chat ping to verify credentials + model availability."""
    protocol = provider.get("protocol", "openai")
    base = provider.get("base_url", "").rstrip("/")
    key = provider.get("api_key", "")
    model = model_id or (provider.get("models") or [{}])[0].get("id", "")
    if not model:
        raise ValueError("no model to test")

    if protocol == "openai":
        url = base + ("/chat/completions" if base.endswith("/v1")
                      else "/v1/chat/completions")
        payload = {"model": model, "messages": [
            {"role": "user", "content": "ping"}], "max_tokens": 8}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % key,
        })
    elif protocol == "anthropic":
        url = base + "/v1/messages"
        payload = {"model": model, "max_tokens": 8,
                   "messages": [{"role": "user", "content": "ping"}]}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % key,
            "x-api-key": key, "anthropic-version": "2023-06-01",
        })
    elif protocol == "gemini":
        url = base + "/v1beta/models/%s:generateContent?key=%s" % (model, key)
        payload = {"contents": [{"parts": [{"text": "ping"}]}],
                   "generationConfig": {"maxOutputTokens": 8}}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json"})
    else:
        raise ValueError("unknown protocol: %s" % protocol)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "model": model,
                "sample": _extract_sample(protocol, data)}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("HTTP %d: %s" % (e.code, detail)) from e


def _extract_sample(protocol: str, data: dict) -> str:
    try:
        if protocol == "openai":
            return (data["choices"][0]["message"].get("content") or "")[:80]
        if protocol == "anthropic":
            return (data["content"][0].get("text") or "")[:80]
        if protocol == "gemini":
            return (data["candidates"][0]["content"]["parts"][0]["text"])[:80]
    except (KeyError, IndexError, TypeError):
        pass
    return ""
