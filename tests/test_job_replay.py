"""Job replay + SSE event ids (2026-11-16): the run outlives the page.

The transcribe page used to own its job and drop the subscription on unmount,
so a run that kept going on the backend simply vanished from the UI. It is now
a VIEW of a module-level session store (app/src/lib/jobSession.ts) whose
subscription lives outside the page, which makes two backend guarantees load
bearing:

1. REPLAY. ``GET /api/jobs/{id}/events`` starts at event 0 and replays the
   buffered history, so a client that attaches late (the page was closed, the
   webview was reloaded) paints the run it never saw. ``GET /api/jobs`` is what
   it asks first: which run, if any, is still queued/running.

2. STABLE INDICES. Every SSE event now carries ``id: <n>`` and the endpoint
   honours ``Last-Event-ID``. Without it the browser's automatic reconnect
   restarts at 0 and the whole log visibly doubles on every hiccup -- tolerable
   for a page-lifetime stream, not for one that now lasts the whole run.

Indices stay absolute even when the buffer is trimmed under ``JOB_EVENT_CAP``
(``Job.dropped``): a resuming client may be handed the tail instead of its
exact record, but it can never be handed the WRONG record.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from mai2srt.config import Config  # noqa: E402
from mai2srt.server import jobs  # noqa: E402
from mai2srt.server.app import create_app  # noqa: E402
from mai2srt.server.jobs import BusyError, JobManager  # noqa: E402


def _cfg(tmp_path) -> Config:
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    return cfg


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(_cfg(tmp_path)))


def _mai_json(tmp_path: Path, name: str = "replay.mai.json") -> Path:
    """Minimal two-speaker transcript: enough for a real (tiny) process job."""
    words = [
        {"text": "こんにちは", "start": 0.0, "end": 0.8, "speaker": "1"},
        {"text": "皆さん", "start": 0.9, "end": 1.4, "speaker": "1"},
        {"text": "はい", "start": 2.1, "end": 2.4, "speaker": "2"},
        {"text": "よろしく", "start": 2.5, "end": 3.1, "speaker": "1"},
    ]
    doc = {"text": "", "language": "ja", "duration": 3.6,
           "engine": "test", "words": words, "utterances": []}
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _settle(mgr: JobManager, work) -> tuple[JobManager, "jobs.Job"]:
    """Run one job to completion and hand back the manager that owns it."""
    async def body():
        job = await mgr.start("run", "test", work)
        for _ in range(200):
            if job.status in ("done", "error", "cancelled"):
                return job
            await asyncio.sleep(0.01)
        raise AssertionError("job never settled")

    return mgr, asyncio.run(body())


def _replay(mgr: JobManager, job, from_idx: int = 0):
    """Collect a finished job's whole buffered stream (what a late client gets).
    The job is settled, so the generator never needs a live event loop."""
    async def go():
        return [(idx, ev) async for idx, ev in mgr.stream(job, from_idx)]

    return asyncio.run(go())


# ------------------------------------------------------------- manager level

def test_stream_yields_one_index_per_event_starting_at_zero():
    async def work(job):
        for i in range(3):
            job.manager.emit(job, "log", {"line": "l%d" % i})
        return {}

    mgr, job = _settle(JobManager(), work)
    seen = [(idx, ev["type"]) for idx, ev in _replay(mgr, job)]
    # three log lines then the terminal event, numbered by position
    assert [idx for idx, _ in seen] == list(range(len(seen)))
    assert [t for _, t in seen] == ["log", "log", "log", "done"]


def test_stream_from_idx_skips_the_head():
    async def work(job):
        for i in range(4):
            job.manager.emit(job, "log", {"line": "l%d" % i})
        return {}

    mgr, job = _settle(JobManager(), work)
    tail = [(idx, ev["data"].get("line")) for idx, ev in _replay(mgr, job, 3)]
    # index 3 is the last log line; the terminal event follows it
    assert tail[0] == (3, "l3")


def test_trimmed_history_keeps_indices_absolute(monkeypatch):
    """A cap must not renumber what survives: the id a browser sends back has
    to mean the same record it meant before the trim."""
    monkeypatch.setattr(jobs, "JOB_EVENT_CAP", 4)

    async def work(job):
        for i in range(9):
            job.manager.emit(job, "log", {"line": "l%d" % i})
        return {}

    mgr, job = _settle(JobManager(), work)
    assert job.dropped > 0                       # the buffer really was trimmed
    assert len(job.events) == jobs.JOB_EVENT_CAP
    # the oldest surviving line keeps the index it always had
    assert job.events[0]["data"]["line"] == "l%d" % job.dropped
    # asking for the trimmed-away beginning lands on the oldest HELD record,
    # never on a wrong one
    assert _replay(mgr, job, 0)[0][0] == job.dropped
    # ... and a resume inside the held window is still exact
    keep = _replay(mgr, job, job.dropped + 1)
    assert keep[0][0] == job.dropped + 1


def test_settled_jobs_are_pruned_running_ones_are_not(monkeypatch):
    monkeypatch.setattr(jobs, "MAX_SETTLED_JOBS", 2)

    async def go():
        mgr = JobManager()

        async def quick(job):
            return {}

        async def long(job):
            await asyncio.sleep(5)

        ids = []
        for _ in range(5):
            j = await mgr.start("process", "t", quick)
            ids.append(j.id)
            await j.task
        live = await mgr.start("run", "long", long)
        # pruning happens at start: only the newest MAX_SETTLED_JOBS survive,
        # plus the running one, which must never be addressable-but-gone
        assert mgr.get(live.id) is live
        assert len(mgr.list()) == 3
        assert mgr.get(ids[0]) is None
        assert mgr.get(ids[-1]) is not None
        mgr.cancel(live.id)
        with contextlib.suppress(asyncio.CancelledError):
            await live.task

    asyncio.run(go())


def test_busy_still_blocks_a_second_run():
    """The store attaches to exactly one job; the mutex it relies on is
    unchanged -- a second click must be refused, not silently queued."""
    async def go():
        mgr = JobManager()

        async def slow(job):
            await asyncio.sleep(5)

        j = await mgr.start("run", "a", slow)
        try:
            await mgr.start("run", "b", slow)
            raised = False
        except BusyError:
            raised = True
        assert raised
        mgr.cancel(j.id)
        with contextlib.suppress(asyncio.CancelledError):
            await j.task

    asyncio.run(go())


# ------------------------------------------------------------------- over HTTP

_IDS = re.compile(r"(?m)^id: (\d+)$")
_EVENTS = re.compile(r"(?m)^event: ")


def test_sse_labels_every_event_and_resumes_from_last_event_id(tmp_path):
    src = _mai_json(tmp_path)
    # The client MUST be lifespan-scoped (`with`). A bare TestClient builds a
    # fresh portal per request and tears its loop down when the response is
    # returned -- which cancels the job task mid-flight and leaves the record
    # ending in `cancelled` with the pipeline's thread still emitting. A real
    # uvicorn server does not do that (a disconnecting client never cancels
    # server-side work); this is purely how the harness fakes a request.
    with _client(tmp_path) as c:
        r = c.post("/api/jobs", json={
            "kind": "process", "mai_json_path": str(src), "use_llm": False,
            "lang": "zh",
        })
        assert r.status_code == 200, r.text
        job_id = r.json()["job"]["id"]

        # the stream ends by itself once the job settles, so a plain GET is the
        # complete replay -- exactly what a client attaching late receives
        body = c.get("/api/jobs/%s/events" % job_id).text
        ids = [int(m) for m in _IDS.findall(body)]
        assert ids, body[:400]
        assert ids == list(range(len(ids)))          # dense, from 0, in order
        assert len(_EVENTS.findall(body)) == len(ids)  # one event line per id
        assert "event: done" in body

        # a resume continues AFTER the id the browser last saw
        resumed = c.get("/api/jobs/%s/events" % job_id,
                        headers={"Last-Event-ID": "1"}).text
        rids = [int(m) for m in _IDS.findall(resumed)]
        assert rids, resumed[:400]
        assert rids[0] == 2
        assert rids == ids[2:]

        # a malformed header must not 500: falling back to from_idx is a valid
        # answer, silence is not
        junk = c.get("/api/jobs/%s/events" % job_id,
                     headers={"Last-Event-ID": "not-a-number"})
        assert junk.status_code == 200
        assert [int(m) for m in _IDS.findall(junk.text)] == ids

        # from_idx still works on its own (the store asks for 0 on a fresh
        # attach, which is how a reloaded page repaints a run it never saw)
        direct = c.get("/api/jobs/%s/events?from_idx=%d" % (job_id, ids[-1])).text
        assert [int(m) for m in _IDS.findall(direct)] == [ids[-1]]
        assert "event: done" in direct


def test_job_list_answers_what_is_still_running(tmp_path):
    """`jobBoot` (jobSession.ts) asks this one question after a reload: which
    run survived the document. A settled job must NOT be presented as live."""
    src = _mai_json(tmp_path)
    with _client(tmp_path) as c:
        job_id = c.post("/api/jobs", json={
            "kind": "process", "mai_json_path": str(src), "use_llm": False,
        }).json()["job"]["id"]
        c.get("/api/jobs/%s/events" % job_id)     # wait for it to settle

        listed = c.get("/api/jobs").json()["jobs"]
        assert [j["id"] for j in listed] == [job_id]
        assert listed[0]["status"] == "done"
        assert listed[0]["title"] == src.name     # the name the folded bar shows
        assert "audio_path" not in listed[0]      # listing stays a summary
