"""The job's two log streams: plain for the user, detail for the bug report.

The UI shows the friendly ``on_log`` lines and hides the diagnostic stream
behind a button -- but the diagnostic stream must run regardless, otherwise
the button has nothing to show when a user finally needs it. It is fed from
the ``mai2srt`` logging tree (see ``jobs._JobDetailHandler``) rather than from
a hand-passed callback, so anything that logs is captured, including code
written later.

These tests pin: detail records reach the running job tagged ``detail``,
plain lines do not, records logged outside a job go nowhere, and a record
raised from a worker thread still lands.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.server import jobs  # noqa: E402


def _run_job(work) -> "jobs.Job":
    async def body():
        manager = jobs.JobManager()
        job = await manager.start("run", "test", work)
        for _ in range(200):
            if job.status in ("done", "error", "cancelled"):
                return job
            await asyncio.sleep(0.01)
        raise AssertionError("job never settled")

    lg = logging.getLogger("mai2srt")
    old = lg.level
    lg.setLevel(logging.INFO)          # _setup_logging does this in the app
    try:
        return asyncio.run(body())
    finally:
        lg.setLevel(old)


def _lines(job, detail: bool) -> list[str]:
    return [e["data"]["line"] for e in job.events
            if e["type"] == "log" and bool(e["data"].get("detail")) is detail]


def test_logging_inside_a_job_becomes_the_detail_stream():
    async def work(job):
        logging.getLogger("mai2srt.media").info(
            "quiet map: peak=-1.3 dB threshold=-31.3 dB gaps=1428")
        job.manager.emit(job, "log", {"line": "切块：3 段，各约 29.0 分钟"})
        return {}

    job = _run_job(work)
    assert job.status == "done"
    detail = _lines(job, True)
    plain = _lines(job, False)
    assert any("quiet map" in line for line in detail)
    # the friendly line stays friendly: it is NOT duplicated into detail
    assert "切块：3 段，各约 29.0 分钟" in plain
    assert not any("切块：" in line for line in detail)


def test_detail_records_carry_a_compact_timestamp():
    async def work(job):
        logging.getLogger("mai2srt.media").info("chunk 0: 0..100s")
        return {}

    job = _run_job(work)
    line = next(l for l in _lines(job, True) if "chunk 0" in l)
    # "12:04:11 INFO  mai2srt.media | chunk 0: 0..100s"
    assert line[2] == ":" and line[5] == ":"
    assert "INFO" in line and "mai2srt.media |" in line


def test_logging_from_a_worker_thread_reaches_the_job():
    async def work(job):
        # asyncio.to_thread copies the context, so the binding survives --
        # this is how prepare()'s ffmpeg logging gets into the stream
        await asyncio.to_thread(
            lambda: logging.getLogger("mai2srt.media").info("from a thread"))
        return {}

    job = _run_job(work)
    assert any("from a thread" in line for line in _lines(job, True))


def test_records_logged_outside_a_job_go_nowhere():
    job = _run_job(lambda job: asyncio.sleep(0))
    before = len(job.events)
    logging.getLogger("mai2srt.media").info("outside any job")
    # nothing was running, so no job collected it (the file log still has it)
    assert len(job.events) == before
    assert not any("outside any job" in e["data"].get("line", "")
                   for e in job.events)


def test_a_failing_job_still_reports_through_both_streams():
    async def work(job):
        raise RuntimeError("boom")

    job = _run_job(work)
    assert job.status == "error"
    errors = [e for e in job.events if e["type"] == "error"]
    assert errors and "boom" in errors[0]["data"]["message"]
    # the manager's own failure line is diagnostic detail, not user copy
    assert any("boom" in line for line in _lines(job, True))
