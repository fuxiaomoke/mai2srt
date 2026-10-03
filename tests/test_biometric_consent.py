"""The 451 biometric-consent gate (issue #1).

playground.microsoft.ai treats audio as biometric data (Illinois BIPA)
and gates uploads behind a one-time account notice. The site shows a
consent dialog on the first audio upload; mai2srt drives the API
headlessly and never sees it, so every upload was hard-rejected with
HTTP 451 {"code":"biometric-consent-required"} -- and _fatal_status_codes
classified 451 as a plain deterministic 4xx, leaving the user with a bare
"not retrying" error.

These tests pin the NEW classification: the biometric gate is neither a
blind retry (pointless) nor a generic fatal -- it is its own error with
guidance text, unless a consent callback accepts on the user's behalf,
in which case the acceptance is POSTed once and the upload retried
immediately.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.transcribe import client
from mai2srt.transcribe.client import (
    BiometricConsentRequired, TranscribeError, _fatal_status_codes,
    is_biometric_rejection,
)

#: the exact shape _upload_once embeds when the server returns the gate
GATE_MSG = ('stage=add create=200 create_body=ok add=451 add_body='
            '{"error":"Biometric notice required before processing this '
            'attachment.","code":"biometric-consent-required",'
            '"kind":"audio-upload"}')


# ------------------------------------------------------------ detection

def test_is_biometric_rejection_matches_gate_body():
    assert is_biometric_rejection(GATE_MSG) is True


def test_is_biometric_rejection_ignores_other_errors():
    assert is_biometric_rejection(
        "stage=add add=413 add_body=Request body too large") is False
    assert is_biometric_rejection(
        "stream HTTP 451: geo-blocked") is False   # 451 without the code
    assert is_biometric_rejection("stream HTTP 500: boom") is False


def test_plain_451_stays_fatal():
    # a 451 that is NOT the biometric gate is still a deterministic 4xx:
    # only the consent-shaped one gets the dedicated path
    assert _fatal_status_codes("stream HTTP 451: geo-blocked") == [451]


# ------------------------------------------------- no callback -> guidance

def test_451_raises_consent_error_without_callback(monkeypatch):
    calls = {"n": 0}

    async def fake_upload(*a, **k):
        calls["n"] += 1
        raise TranscribeError(GATE_MSG)

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    monkeypatch.setattr(client, "RETRY_BACKOFF_S", (0, 0, 0))

    raised: Exception | None = None
    try:
        asyncio.run(client.transcribe_file(None, Path("x.mp3"), "audio/mpeg"))
    except BiometricConsentRequired as e:
        raised = e
    assert raised is not None
    # guidance names BOTH entry points (CLI + GUI)
    assert "mai2srt consent" in str(raised)
    assert "接受生物特征通知" in str(raised)
    # and no blind re-uploads of a gated body
    assert calls["n"] == 1


# ------------------------------------------- callback accepts -> retry wins

def test_consent_acceptance_retries_upload_immediately(monkeypatch):
    uploads = {"n": 0}
    accepted = {"n": 0}
    asked = {"n": 0}

    async def fake_upload(*a, **k):
        uploads["n"] += 1
        if uploads["n"] == 1:
            raise TranscribeError(GATE_MSG)
        return "ok"

    async def fake_accept(*a, **k):
        accepted["n"] += 1
        return {"accepted": True}

    async def agree():
        asked["n"] += 1
        return True

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    monkeypatch.setattr(client, "accept_biometric_consent", fake_accept)
    monkeypatch.setattr(client, "RETRY_BACKOFF_S", (0, 0, 0))

    assert asyncio.run(client.transcribe_file(
        None, Path("x.mp3"), "audio/mpeg", consent=agree)) == "ok"
    assert asked["n"] == 1 and accepted["n"] == 1
    assert uploads["n"] == 2   # gate once, success once


def test_consent_declined_raises_with_guidance(monkeypatch):
    uploads = {"n": 0}

    async def fake_upload(*a, **k):
        uploads["n"] += 1
        raise TranscribeError(GATE_MSG)

    async def decline():
        return False

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    monkeypatch.setattr(client, "RETRY_BACKOFF_S", (0, 0, 0))

    raised: Exception | None = None
    try:
        asyncio.run(client.transcribe_file(
            None, Path("x.mp3"), "audio/mpeg", consent=decline))
    except BiometricConsentRequired as e:
        raised = e
    assert raised is not None
    assert uploads["n"] == 1   # declined -> no retry at all


def test_451_after_acceptance_terminates(monkeypatch):
    """A second 451 after the user already accepted must not re-prompt or
    loop -- it surfaces the guidance error (something else is wrong)."""
    uploads = {"n": 0}
    accepted = {"n": 0}
    asked = {"n": 0}

    async def fake_upload(*a, **k):
        uploads["n"] += 1
        raise TranscribeError(GATE_MSG)

    async def fake_accept(*a, **k):
        accepted["n"] += 1
        return {"accepted": True}

    async def agree():
        asked["n"] += 1
        return True

    monkeypatch.setattr(client, "_upload_once", fake_upload)
    monkeypatch.setattr(client, "accept_biometric_consent", fake_accept)
    monkeypatch.setattr(client, "RETRY_BACKOFF_S", (0, 0, 0))

    raised: Exception | None = None
    try:
        asyncio.run(client.transcribe_file(
            None, Path("x.mp3"), "audio/mpeg", consent=agree))
    except BiometricConsentRequired as e:
        raised = e
    assert raised is not None
    assert asked["n"] == 1 and accepted["n"] == 1
    assert uploads["n"] == 2   # gate, gated-again -- then the raise


# --------------------------------------------- exception is catchable as TranscribeError

def test_consent_error_is_a_transcribe_error():
    # the CLI/server error handlers catch TranscribeError; the new type
    # must flow through them unchanged
    assert issubclass(BiometricConsentRequired, TranscribeError)
