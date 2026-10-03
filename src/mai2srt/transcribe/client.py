"""Playground API client — the M0-validated call chain, hardcoded.

Protocol facts (all verified by the 2026-09-22 spike, see RESEARCH.md):
- tRPC batch format: POST /api/trpc/<proc>?batch=1 with body {"0": {…}}
  (NO superjson wrapper)
- audio upload is base64-in-JSON via conversations.addAudioPromptAndInitResponse
- /api/chat/stream (SSE) emits {rich_transcription} among its data events;
  an x-request-id header is expected
- the backend silently strips unknown fields; the only accepted knob is
  {diarize}, and it is ONE-WAY: sending false (or omitting it) still
  returns diarized output (live-verified 2026-09-26) -- speaker
  separation cannot be disabled server-side
- server-side gate: .mp3/.wav only, 25 MiB / 3615 s limits (on the RAW
  file, same as the site's own picker; a 23 MiB file = ~30.7 MiB base64
  transcribes fine)
- conversations.delete cleans the account after use
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page

from ..config import (
    BASE_URL, MAX_UPLOAD_BYTES, MODEL_ID, RETRY_BACKOFF_S, UPLOAD_ATTEMPTS,
)

log = logging.getLogger("mai2srt.client")

TRPC = f"{BASE_URL}/api/trpc"

#: replaced by the live conversation id inside the page JS
CONV_TOKEN = "__MAI2SRT_CONV_ID__"


class TranscribeError(RuntimeError):
    """One upload attempt failed; message carries the stage + raw body."""


class BiometricConsentRequired(TranscribeError):
    """The account has not accepted the playground biometric notice.

    Raised on the 451 ``biometric-consent-required`` gate (audio counts as
    biometric data under BIPA): no retry can fix it until the acceptance
    is recorded server-side. The message tells the user where to accept.
    """


#: the one consent kind mai2srt needs: uploading an audio file
CONSENT_KIND = "audio-upload"


def is_biometric_rejection(msg: str) -> bool:
    """True when an _upload_once error message is the biometric gate.

    The playground rejects gated audio with HTTP 451 + code
    ``biometric-consent-required`` in the body; _upload_once embeds the
    raw body in its TranscribeError, so the code string is the marker.
    """
    return "biometric-consent-required" in msg


@dataclass
class UploadResult:
    rich: dict
    conversation_id: str
    request_id: str | None
    #: None = keep requested (no cleanup attempted); True/False = the
    #: post-transcription conversation delete succeeded / failed. Surfaced
    #: because "leave no trace" is a promise the user must be able to trust.
    cleanup_ok: bool | None = None


# --------------------------------------------------------------------------
# request builders
# --------------------------------------------------------------------------

def _create_body(title: str) -> dict:
    return {"0": {"title": title, "modelId": MODEL_ID}}


def _add_body(conversation_id: str, audio_b64: str, mime: str,
              name: str) -> dict:
    return {"0": {
        "conversationId": conversation_id,
        "modelId": MODEL_ID,
        "audioBase64": audio_b64,
        "audioMimeType": mime,
        "audioFileName": name,
        "lastMessageId": None,
    }}


def _stream_body(conversation_id: str, pending_model_message_id: str,
                 diarize: bool) -> dict:
    # ALWAYS send the explicit boolean: an absent/empty transcribeOptions
    # makes the server default to diarize ON (live-verified 2026-09-26:
    # --no-diarize used to send {} and still got 2 speakers back)
    return {
        "conversationId": conversation_id,
        "modelId": MODEL_ID,
        "pendingModelMessageId": pending_model_message_id,
        "isFirstMessage": True,
        "transcribeOptions": {"diarize": diarize},
    }


def _delete_body(conversation_id: str) -> dict:
    return {"0": {"id": conversation_id}}


# --------------------------------------------------------------------------
# in-page JS
# --------------------------------------------------------------------------

CREATE_ADD_JS = """
async (args) => {
  // every post carries a hard timeout: an unbounded fetch on an oversized
  // or wedged request hung page.evaluate forever (the 2026-09-26 incident
  // showed up as "transcription never returns"); status 0 = aborted
  const post = async (url, body) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort('timeout'), args.timeoutMs);
    try {
      const r = await fetch(url, {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body), signal: ctl.signal});
      return {status: r.status, text: await r.text()};
    } catch (e) {
      return {status: 0, text: String(e && e.message || e)};
    } finally { clearTimeout(timer); }
  };
  const findConversationId = (obj) => {
    const stack = [obj];
    while (stack.length) {
      const cur = stack.shift();
      if (cur && typeof cur === 'object') {
        if (typeof cur.id === 'string' && (cur.title !== undefined || cur.name !== undefined)) return cur.id;
        if (typeof cur.conversationId === 'string') return cur.conversationId;
        for (const v of Object.values(cur)) stack.push(v);
      }
    }
    return null;
  };
  const create = await post(args.createUrl, args.createBody);
  if (create.status !== 200) return {stage: 'create', create, convId: null};
  let convId = null;
  try { convId = findConversationId(JSON.parse(create.text)); } catch (e) {}
  if (!convId) return {stage: 'create-id', create, convId: null};
  const addBody = JSON.parse(
    JSON.stringify(args.addBody).split('__MAI2SRT_CONV_ID__').join(convId));
  const add = await post(args.addUrl, addBody);
  return {stage: add.status === 200 ? 'ready' : 'add', create, add, convId};
}
"""

STREAM_JS = """
async (args) => {
  // idle watchdog, not an absolute cap: a 60-min transcription may stream
  // for many minutes, but SILENCE means the request is wedged -- abort so
  // the retry loop (and the user) get an error instead of a hang
  const ctl = new AbortController();
  let idleTimer = null;
  const kick = () => {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => ctl.abort('stream idle'), args.idleMs);
  };
  kick();
  try {
    const headers = {'Content-Type': 'application/json',
                     'x-request-id': args.requestId};
    const r = await fetch(args.streamUrl, {method: 'POST', headers,
      body: JSON.stringify(args.streamBody), signal: ctl.signal});
    const status = r.status;
    let sse = '';
    if (r.body) {
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        kick();
        sse += dec.decode(value, {stream: true});
      }
      sse += dec.decode();
    } else {
      sse = await r.text();
    }
    return {status, sse};
  } catch (e) {
    return {status: 0, sse: String(e && e.message || e)};
  } finally {
    if (idleTimer) clearTimeout(idleTimer);
  }
}
"""

DELETE_JS = """
async (a) => {
  const r = await fetch(a.url, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(a.body)});
  return {status: r.status, text: (await r.text()).slice(0, 500)};
}
"""

#: one fetch for both consent verbs (issue #1: GET reports whether the
#: account still owes the biometric notice, POST records the acceptance)
CONSENT_JS = """
async (a) => {
  const r = await fetch(a.url, {method: a.method,
    headers: {'Content-Type': 'application/json'},
    body: a.body === null ? undefined : JSON.stringify(a.body)});
  return {status: r.status, text: (await r.text()).slice(0, 500)};
}
"""


# --------------------------------------------------------------------------
# response helpers
# --------------------------------------------------------------------------

def _deep_find(obj, pred):
    if isinstance(obj, dict):
        if pred(obj):
            return obj
        for v in obj.values():
            r = _deep_find(v, pred)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _deep_find(v, pred)
            if r is not None:
                return r
    return None


def parse_sse(text: str) -> dict:
    """SSE -> {request_id, warnings[], rich, segments, text, data_events}."""
    out: dict = {"request_id": None, "warnings": [], "rich": None,
                 "segments": None, "text": "", "data_events": 0}
    parts: list[str] = []
    pending_event = None
    for line in text.splitlines():
        if not line or line == "\r":
            pending_event = None
            continue
        if line.startswith("event: "):
            pending_event = line[7:].strip()
            continue
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if pending_event == "request_id":
            try:
                out["request_id"] = (json.loads(data) or {}).get("requestId")
            except Exception:  # noqa: BLE001
                pass
            pending_event = None
            continue
        if pending_event == "warning":
            out["warnings"].append(data[:500])
            pending_event = None
            continue
        if data == "[DONE]":
            continue
        try:
            j = json.loads(data)
        except Exception:  # noqa: BLE001
            continue
        out["data_events"] += 1
        if not isinstance(j, dict):
            continue
        if isinstance(j.get("rich_transcription"), dict):
            out["rich"] = j["rich_transcription"]
        if isinstance(j.get("transcription_segments"), list):
            out["segments"] = j["transcription_segments"]
        if isinstance(j.get("delta"), str) and j.get("type") == "text":
            parts.append(j["delta"])
        else:
            ch = j.get("choices")
            if isinstance(ch, list) and ch:
                delta = (ch[0] or {}).get("delta") or {}
                if isinstance(delta.get("content"), str):
                    parts.append(delta["content"])
    out["text"] = "".join(parts)
    return out


# --------------------------------------------------------------------------
# the biometric-consent gate (issue #1)
# --------------------------------------------------------------------------

async def _consent_fetch(page: Page, method: str, kind: str) -> dict:
    res = await page.evaluate(CONSENT_JS, {
        "url": (f"{BASE_URL}/api/biometric-consent?kind={kind}"
                if method == "GET" else f"{BASE_URL}/api/biometric-consent"),
        "method": method,
        "body": None if method == "GET" else {"kind": kind},
    })
    status = res.get("status")
    text = str(res.get("text"))[:500]
    if status != 200:
        raise TranscribeError(
            f"consent {method} HTTP {status}: {text}")
    try:
        return json.loads(res.get("text") or "{}")
    except ValueError:
        raise TranscribeError(f"consent {method} returned non-JSON: {text}") from None


async def consent_status(page: Page, kind: str = CONSENT_KIND) -> dict:
    """GET /api/biometric-consent -> {"needed":bool,"jurisdiction":...}."""
    return await _consent_fetch(page, "GET", kind)


async def accept_biometric_consent(page: Page, kind: str = CONSENT_KIND) -> dict:
    """POST /api/biometric-consent — the same call the site's own consent
    dialog makes. The acceptance lives on the account server-side, so it
    outlives this page/session and unblocks headless uploads for good."""
    return await _consent_fetch(page, "POST", kind)


# --------------------------------------------------------------------------
# the upload flow
# --------------------------------------------------------------------------

async def _upload_once(page: Page, audio: Path, mime: str, *,
                       diarize: bool, keep: bool, title: str) -> UploadResult:
    audio_b64 = audio.read_bytes()
    import base64
    b64 = base64.b64encode(audio_b64).decode("ascii")
    name = audio.name
    # belt-and-suspenders gate on the RAW size (the site's own limit --
    # the base64 inflation does not count against it, live-verified):
    # prepare()/compression should make this unreachable; refuse rather
    # than send a body the server will bounce or wedge on
    if len(audio_b64) + 4096 > MAX_UPLOAD_BYTES:
        raise TranscribeError(
            f"audio is %.1f MiB raw, over the %.0f MiB upload limit — "
            "size-budget bug, please report"
            % (len(audio_b64) / 1024 / 1024,
               MAX_UPLOAD_BYTES / 1024 / 1024))
    log.info("uploading %s (%.1f MiB b64, %s)", name,
             len(b64) / 1024 / 1024, mime)

    res1 = await page.evaluate(CREATE_ADD_JS, {
        "createUrl": f"{TRPC}/conversations.create?batch=1",
        "createBody": _create_body(title),
        "addUrl": f"{TRPC}/conversations.addAudioPromptAndInitResponse?batch=1",
        "addBody": _add_body(CONV_TOKEN, b64, mime, name),
        "timeoutMs": 240_000,
    })
    stage = res1.get("stage")
    create = res1.get("create") or {}
    add = res1.get("add") or {}
    if stage != "ready":
        raise TranscribeError(
            f"stage={stage} create={create.get('status')} "
            f"create_body={str(create.get('text'))[:200]} "
            f"add={add.get('status')} add_body={str(add.get('text'))[:300]}")
    conv_id = res1.get("convId")

    add_json = json.loads(add.get("text") or "{}")
    d = _deep_find(add_json,
                   lambda x: "userMessage" in x and "modelMessage" in x)
    model_msg_id = ((d or {}).get("modelMessage") or {}).get("id") if d else None
    if not model_msg_id:
        raise TranscribeError(
            f"cannot find modelMessage.id in add response: "
            f"{str(add.get('text'))[:300]}")

    request_id = str(uuid.uuid4())
    res2 = await page.evaluate(STREAM_JS, {
        "streamUrl": f"{BASE_URL}/api/chat/stream",
        "streamBody": _stream_body(conv_id, model_msg_id, diarize),
        "requestId": request_id,
        "idleMs": 180_000,
    })
    if res2["status"] != 200:
        raise TranscribeError(
            f"stream HTTP {res2['status']}: {res2['sse'][:300]}")
    parsed = parse_sse(res2["sse"])
    if parsed["rich"] is None:
        raise TranscribeError(
            "no rich_transcription in stream "
            f"(events={parsed['data_events']}, warnings={parsed['warnings']}, "
            f"text={parsed['text'][:200]})")

    cleanup_ok: bool | None = None
    if not keep:
        try:
            dres = await page.evaluate(DELETE_JS, {
                "url": f"{TRPC}/conversations.delete?batch=1",
                "body": _delete_body(conv_id),
            })
            cleanup_ok = dres.get("status") == 200
            if cleanup_ok:
                log.info("cleanup delete -> 200 (conversation removed)")
            else:
                log.warning("cleanup delete -> %s: %s",
                            dres.get("status"), str(dres.get("text"))[:200])
        except Exception as e:  # noqa: BLE001 — cleanup is best effort
            cleanup_ok = False
            log.warning("cleanup delete failed (non-fatal): %s", e)

    return UploadResult(rich=parsed["rich"], conversation_id=conv_id,
                        request_id=parsed["request_id"], cleanup_ok=cleanup_ok)


#: 4xx codes worth another attempt: auth (reauth flow handles 401/403 and
#: retries immediately; without reauth they fall back to the blind retry,
#: the historic behaviour) and the transient-by-definition 408/429
_RETRYABLE_4XX = {401, 403, 408, 429}


def _fatal_status_codes(msg: str) -> list[int]:
    """HTTP codes embedded in an _upload_once error message that make the
    failure deterministic (retrying just re-sends the same 20+ MiB body)."""
    codes = [int(c) for c in re.findall(r"(?:add|create)=(\d{3})", msg)]
    codes += [int(c) for c in re.findall(r"stream HTTP (\d{3})", msg)]
    return [c for c in codes if 400 <= c < 500 and c not in _RETRYABLE_4XX]


async def transcribe_file(page: Page, audio: Path, mime: str, *,
                          diarize: bool = True, keep: bool = False,
                          title: str | None = None,
                          reauth=None, consent=None) -> UploadResult:
    """Upload one prepared file with retries; ``reauth`` is an async callable
    (usually browser.ensure_auth) invoked when a 401-shaped failure occurs.

    ``consent`` is an async callable returning True when the user agreed to
    accept the playground biometric notice. It is asked at most once, when
    the 451 biometric gate rejects the upload; on True the acceptance is
    recorded in-page and the upload retried immediately. None/False (or a
    second 451) raises BiometricConsentRequired with guidance text.
    """
    title = title or f"mai2srt {time.strftime('%m%d_%H%M%S')}"
    last: Exception | None = None
    asked_consent = False
    for attempt in range(UPLOAD_ATTEMPTS):
        try:
            return await _upload_once(page, audio, mime,
                                      diarize=diarize, keep=keep, title=title)
        except TranscribeError as e:
            last = e
            msg = str(e)
            log.warning("attempt %d/%d failed: %s",
                        attempt + 1, UPLOAD_ATTEMPTS, msg[:300])
            # the 451 biometric gate FIRST: it is neither retryable nor a
            # plain fatal 4xx -- the account owes a one-time acceptance,
            # and only the user can give it (it is a legal notice)
            if is_biometric_rejection(msg):
                if consent is not None and not asked_consent:
                    asked_consent = True
                    if await consent():
                        log.info("user accepted the biometric notice; "
                                 "recording it and retrying immediately")
                        await accept_biometric_consent(page)
                        continue
                raise BiometricConsentRequired(
                    "playground 要求当前账号先接受一次生物特征通知"
                    "（音频按生物特征数据处理，BIPA）才能上传：\n"
                    "  CLI: 重新运行并按提示接受，或先执行 mai2srt consent\n"
                    "  桌面端: 设置 → 文件与账号 → 「接受生物特征通知」后重试\n"
                    "(the account must accept the playground biometric "
                    "notice once before audio uploads: run `mai2srt "
                    "consent`, or Settings → accept biometric notice in "
                    "the GUI, then retry)") from e
            # auth failures get their dedicated second chance FIRST --
            # the fatal gate below must never short-circuit a re-login
            if reauth is not None and ("401" in msg or "403" in msg):
                status = await reauth()
                if status == 200:
                    log.info("re-authenticated; retrying immediately")
                    continue
            fatal = _fatal_status_codes(msg)
            if fatal:
                # deterministic rejection (413 etc.): never succeeds on
                # retry -- fail now instead of re-uploading for minutes
                raise TranscribeError(
                    f"server rejected the upload (HTTP {fatal[0]}), not "
                    f"retrying: {msg[:400]}") from e
            if attempt < UPLOAD_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
    raise TranscribeError(f"all {UPLOAD_ATTEMPTS} attempts failed: {last}")
