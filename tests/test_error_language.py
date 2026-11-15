"""Errors the user reads are one sentence in their language, not a dump.

Exceptions carry their technical English message as ``str(exc)`` -- raw
service bodies, ffmpeg stderr -- and that is what the file log and the detail
stream keep. A ``code`` adds the short translated sentence the UI shows
instead. Without this, a failed run put strings like
``stage=add create=200 ... add_body={...}`` in the error box, and a half
Chinese / half English guidance blob for the biometric gate.

The fallback matters as much as the translation: an exception with no code
must still show its real message rather than a vague "something failed".
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.media.probe import MediaError  # noqa: E402
from mai2srt.messages import normalize, tr, user_message  # noqa: E402
from mai2srt.server import jobs  # noqa: E402
from mai2srt.transcribe.client import (  # noqa: E402
    BiometricConsentRequired, TranscribeError,
)


# ------------------------------------------------------------- the mapping

def test_coded_error_is_translated():
    e = TranscribeError("stream HTTP 422: {...}", code="err.stream_failed",
                        status=422)
    assert "422" in user_message(e, "zh")
    assert "转录请求" in user_message(e, "zh")
    assert user_message(e, "en") == tr("en", "err.stream_failed", status=422)
    # the technical message survives untouched for the log
    assert str(e) == "stream HTTP 422: {...}"


def test_parameterless_coded_error():
    e = TranscribeError("not signed in", code="err.not_signed_in")
    assert user_message(e, "zh") == "登录失效：请先在设置页重新登录"
    assert "sign in" in user_message(e, "en")


def test_uncoded_error_shows_its_own_message():
    e = RuntimeError("[Errno 13] Permission denied: 'out.srt'")
    assert user_message(e, "zh") == str(e)
    assert user_message(e, "en") == str(e)


def test_unknown_code_falls_back_to_the_message():
    # a typo in a code must not swallow the reason
    e = TranscribeError("raw detail", code="err.no_such_thing")
    assert user_message(e, "en") == "raw detail"


def test_media_error_carries_a_code_too():
    e = MediaError("ffmpeg mp3 encode failed: boom", code="err.encode_failed")
    assert user_message(e, "en") == tr("en", "err.encode_failed")
    assert user_message(e, "zh") == tr("zh", "err.encode_failed")


def test_consent_gate_has_a_readable_error():
    e = BiometricConsentRequired("long bilingual blob", code="err.consent_required")
    zh, en = user_message(e, "zh"), user_message(e, "en")
    assert "生物特征通知" in zh and "mai2srt consent" in zh
    assert "biometric notice" in en and "mai2srt consent" in en


def test_loose_language_tags_work_here_too():
    e = TranscribeError("x", code="err.not_signed_in")
    assert user_message(e, "en-US") == user_message(e, "en")
    assert normalize("en-US") == "en"


# ------------------------------------------------- the job error event

def _run_failing_job(exc: BaseException, lang: str = "zh"):
    async def body():
        manager = jobs.JobManager()

        async def work(job):
            raise exc

        job = await manager.start("run", "t", work, lang=lang)
        for _ in range(200):
            if job.status in ("done", "error", "cancelled"):
                return job
            await asyncio.sleep(0.01)
        raise AssertionError("job never settled")

    logger = logging.getLogger("mai2srt")
    old = logger.level
    logger.setLevel(logging.INFO)
    try:
        return asyncio.run(body())
    finally:
        logger.setLevel(old)


def test_error_event_carries_the_translated_sentence():
    job = _run_failing_job(
        TranscribeError("stage=add create=200 add=451 add_body={\"code\":\"x\"}",
                        code="err.upload_step_failed", status=451),
        lang="en")
    assert job.status == "error"
    assert "refused the upload step" in job.error
    assert "HTTP 451" in job.error
    assert "stage=add" not in job.error            # no protocol dump for the user
    ev = [e for e in job.events if e["type"] == "error"][0]
    assert ev["data"]["message"] == job.error


def test_the_technical_message_still_reaches_the_detail_stream():
    job = _run_failing_job(
        TranscribeError("stage=add add=451 add_body=biometric-consent-required",
                        code="err.upload_step_failed", status=451),
        lang="zh")
    detail = [e["data"]["line"] for e in job.events
              if e["type"] == "log" and e["data"].get("detail")]
    assert any("stage=add" in line for line in detail)


def test_uncoded_job_error_keeps_its_message():
    job = _run_failing_job(OSError("disk full"), lang="en")
    assert job.error == "disk full"


def test_error_language_follows_the_job():
    exc1 = TranscribeError("x", code="err.no_audio_stream")
    assert _run_failing_job(exc1, lang="zh").error == tr("zh", "err.no_audio_stream")
    exc2 = TranscribeError("x", code="err.no_audio_stream")
    assert _run_failing_job(exc2, lang="en").error == tr("en", "err.no_audio_stream")


@pytest.mark.parametrize("lang,needle", [("zh", "没有音频轨"), ("en", "no audio track")])
def test_translation_is_really_language_specific(lang, needle):
    e = TranscribeError("no audio stream in x", code="err.no_audio_stream")
    assert needle in user_message(e, lang)


# ------------------------------------------------- unwritable output / bad input

def test_unwritable_output_names_the_file(tmp_path):
    from mai2srt import runner

    target = tmp_path / "missing-dir" / "out.srt"
    with pytest.raises(TranscribeError) as ei:
        runner._write_text(target, "hello")
    assert ei.value.code == "err.file_write"
    # the user sees WHICH file, not just "[Errno 2] No such file or directory"
    assert "out.srt" in user_message(ei.value, "zh")
    assert "out.srt" in user_message(ei.value, "en")
    assert "No such file" in str(ei.value)      # the technical text survives


def test_bad_json_error_is_readable():
    e = TranscribeError('cannot read x.mai.json: Expecting value: line 1',
                        code="err.bad_json", path="x.mai.json")
    assert "mai.json" in user_message(e, "zh")
    assert "cannot be read" in user_message(e, "en")
