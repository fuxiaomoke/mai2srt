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
from ..segment.llm import LLMEndpoint, _build_request, _content, _openai_path

PROTOCOLS = ("openai", "anthropic", "gemini")
AUTH_STYLES = ("bearer", "x-api-key", "query-key", "none")

#: one-click presets (id -> display info). ``urls`` maps each API format
#: the vendor ACTUALLY serves to its default endpoint -- its keys are
#: exactly the formats the add-provider card may offer, and ``protocol``
#: is the initially selected one. Verified against the vendors' docs in
#: 2026-09:
#:   DeepSeek      https://api.deepseek.com/anthropic (Anthropic-format page)
#:   OpenAI        OpenAI format only
#:   Anthropic     Anthropic format only
#:   Gemini        native + OpenAI-compat layer at /v1beta/openai (an
#:                 extra /v1 after it 404s -- see _openai_path)
#:   OpenRouter    https://openrouter.ai/api (Anthropic-format page)
#:   Ollama        Anthropic Messages API compat since v0.14.0 (2026-01);
#:                 local auth is ignored, "none" keeps the key optional
PRESETS = {
    "deepseek":   {"name": "DeepSeek 官方", "protocol": "openai",
                   "auth_style": "bearer",
                   "urls": {"openai": "https://api.deepseek.com",
                            "anthropic": "https://api.deepseek.com/anthropic"}},
    "openai":     {"name": "OpenAI", "protocol": "openai",
                   "auth_style": "bearer",
                   "urls": {"openai": "https://api.openai.com/v1"}},
    "anthropic":  {"name": "Anthropic Claude", "protocol": "anthropic",
                   "auth_style": "x-api-key",
                   "urls": {"anthropic": "https://api.anthropic.com"}},
    "gemini":     {"name": "Google Gemini", "protocol": "gemini",
                   "auth_style": "query-key",
                   "urls": {"gemini": "https://generativelanguage.googleapis.com",
                            "openai": "https://generativelanguage.googleapis.com/v1beta/openai"}},
    "openrouter": {"name": "OpenRouter", "protocol": "openai",
                   "auth_style": "bearer",
                   "urls": {"openai": "https://openrouter.ai/api/v1",
                            "anthropic": "https://openrouter.ai/api"}},
    "ollama":     {"name": "Ollama (local)", "protocol": "openai",
                   "auth_style": "none",
                   "urls": {"openai": "http://127.0.0.1:11434/v1",
                            "anthropic": "http://127.0.0.1:11434"}},
    "custom":     {"name": "自定义 OpenAI 兼容", "label": "自定义",
                   "protocol": "openai", "auth_style": "bearer",
                   "urls": {"openai": "", "anthropic": "", "gemini": ""}},
}

#: built-in context/output hints for common model families (tokens). Used ONLY
#: when the provider's own /models endpoint reports no limits -- DeepSeek,
#: Gemini and OpenRouter DO report them, bare-id relays do not. A stale value
#: can only shift how batches are packed (segment/llm.py), never corrupt a
#: result, so unknown families stay conservative. Verified against the
#: vendors' docs in 2026-09; re-verify before trusting a number here.
_BUILTIN_META: list[tuple[str, int, int]] = [
    (r"deepseek.*(v4|flash)", 1_048_576, 393_216),
    (r"deepseek.*(pro|reasoner|r1)", 1_048_576, 393_216),
    (r"gpt-6", 1_050_000, 128_000),
    (r"gpt-5\.[4-9]", 400_000, 128_000),
    (r"gpt-5", 400_000, 128_000),
    (r"gpt-4\.1", 1_000_000, 32_768),
    (r"gpt-4o", 128_000, 16_384),
    (r"o[34](-mini)?", 200_000, 100_000),
    (r"claude.*(fable-5|opus-5|sonnet-5)", 1_000_000, 128_000),
    (r"claude.*haiku-4-5", 200_000, 64_000),
    (r"claude.*(-4-6|4-6|opus-4-6)", 200_000, 64_000),
    (r"claude.*sonnet-4-5", 200_000, 64_000),
    (r"claude.*haiku", 200_000, 32_768),
    (r"claude", 200_000, 64_000),
    (r"gemini-3\.[0-9]+-flash", 1_048_576, 65_536),
    (r"gemini.*pro", 1_048_576, 65_536),
    # thinking-era Gemini catch-all: covers version-less aliases relays
    # invent ("gemini-3-flash", "gemini-2.5-flash") that the two rows
    # above miss; 1M/64K holds across the whole 2.5+ line
    (r"gemini-(2\.5|[3-9])", 1_048_576, 65_536),
    (r"glm-5\.[0-9]+", 1_000_000, 131_072),
    (r"glm", 128_000, 32_768),
    (r"qwen3\.[0-9]+-max", 1_000_000, 131_072),
    (r"qwen3", 131_072, 8_192),
    (r"grok-4\.[0-9]+", 500_000, 128_000),
    (r"kimi.*k3", 1_048_576, 131_072),
    (r"minimax-m3", 1_000_000, 524_288),
    (r"minimax", 1_000_000, 204_800),
]

_REASONING_RE = re.compile(
    r"r1|reasoner|o[134](-mini|-pro)?(\b|-)|think|max|ultra|deepseek-v4|"
    r"gpt-5|deepseek-flash|deepseek-pro|"
    # Claude: thinking-capable since 3.7. Both wild naming shapes appear --
    # generation-first (claude-3-7-sonnet, claude-4-1) and family-first
    # (claude-opus-4-6). The (?!3) guard keeps the non-thinking 3.0/3.5
    # generation out: "claude-3-5-sonnet" must not sneak in via its "-5".
    r"claude-(?:3[-.]7|[4-9])(\b|-)|claude-(?!3)\w+-[4-9]|"
    # Gemini: thinking since 2.5 (official thinking doc: "Gemini 3 and 2.5
    # series models use a thinking process"); 1.5/2.0 stay out
    r"gemini-(2\.5|[3-9])", re.I)
_VISION_RE = re.compile(r"vl|4o(\b|-)|gemini|claude|glm-4v|qwen-vl|vision", re.I)

EFFORT_LEVELS = ("off", "low", "medium", "high", "max")


def tag_model(model_id: str, api_meta: dict | None = None) -> dict:
    """Heuristic capability tagging; API-provided fields win when present.

    ``limits_source`` records WHERE the numbers came from, because the two
    differ in trustworthiness: "api" = the provider reported them (DeepSeek,
    Gemini, OpenRouter), "builtin" = matched a name pattern in the table
    below, None = unknown (batching then assumes a conservative floor).
    """
    meta = {
        "id": model_id,
        "context_window": None,
        "max_output": None,
        "reasoning": bool(_REASONING_RE.search(model_id)),
        "vision": bool(_VISION_RE.search(model_id)),
        "efforts": None,
        "limits_source": None,
        "source": "discovered",
    }
    for pat, ctx, out in _BUILTIN_META:
        if re.search(pat, model_id, re.I):
            meta["context_window"] = ctx
            meta["max_output"] = out
            meta["limits_source"] = "builtin"
            break
    if api_meta:
        from_api = False
        for k_src, k_dst in (("context_length", "context_window"),
                             ("context_window", "context_window"),
                             ("max_output_tokens", "max_output")):
            v = api_meta.get(k_src)
            if isinstance(v, int) and v > 0:
                meta[k_dst] = v
                from_api = True
        # modalities and effort levels are DECLARED, not guessed: the name
        # heuristics above miss them (e.g. deepseek-flash reports image input
        # while _VISION_RE has no "deepseek")
        in_mod = api_meta.get("input_modalities")
        if isinstance(in_mod, list) and in_mod:
            meta["vision"] = any(
                str(m).lower() in ("image", "video") for m in in_mod)
            from_api = True
        effort = api_meta.get("effort")
        if isinstance(effort, dict):
            levels = [str(x) for x in (effort.get("supported_levels") or []) if x]
            if levels:
                meta["efforts"] = levels
                # advertised effort levels mean a thinking-capable model
                if any(str(x).lower() not in ("off", "none") for x in levels):
                    meta["reasoning"] = True
                from_api = True
        if from_api:
            meta["limits_source"] = "api"
    if meta["reasoning"] and not meta["efforts"]:
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
        url = _openai_path(base, "models")
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
                # api fields win when a relay reports limits beyond the id
                out.append(tag_model(mid, m if isinstance(m, dict) else None))
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
            keys = ("context_window", "max_output", "reasoning", "vision",
                    "efforts", "limits_source")
            delta = {k: {"from": old[mid].get(k), "to": m.get(k)}
                     for k in keys if old[mid].get(k) != m.get(k)}
            if delta:
                changed.append({"id": mid, "changes": delta})
    return {"added": added, "removed": removed, "changed": changed}


def test_provider(provider: dict, model_id: str | None = None,
                  effort: str | None = None, timeout: float = 60.0) -> dict:
    """Minimal chat ping to verify credentials + model availability.

    Built by the SAME request builder as the real segmentation calls --
    auth headers, User-Agent, effort/thinking parameters -- so a green test
    means the exact request shape a job will send is accepted (this is how
    a relay-side "thinking column stays blank" report gets diagnosed:
    before, the ping never carried the thinking parameter at all).
    """
    protocol = provider.get("protocol", "openai")
    model = model_id or (provider.get("models") or [{}])[0].get("id", "")
    if not model:
        raise ValueError("no model to test")
    meta = next((m for m in provider.get("models", [])
                 if isinstance(m, dict) and m.get("id") == model), {})
    endpoint = LLMEndpoint(
        protocol=protocol,
        base_url=provider.get("base_url", ""),
        api_key=provider.get("api_key", ""),
        model=model, effort=effort,
        context_window=meta.get("context_window"),
        max_output=meta.get("max_output"))
    # no explicit generation cap: a ping must not depend on cap-field
    # support (jobs only send one once a batch could outgrow a default)
    url, payload, headers = _build_request(
        endpoint, "You are a connectivity probe. Reply with: pong", "ping")

    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "model": model,
                "sample": _content(data, protocol)[:80]}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise ValueError("HTTP %d: %s" % (e.code, detail)) from e
