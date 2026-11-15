"""The interface language reaches the job that formats the user log.

The GUI sends ``lang`` when it creates a job; the server must pin that
language onto the job's own Config copy, so a run keeps one language even if
the user flips the switch in Settings while it is in flight. Without this the
catalog was correct and the app still printed the wrong language.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from mai2srt import runner  # noqa: E402
from mai2srt.config import Config  # noqa: E402
from mai2srt.server.app import create_app  # noqa: E402


def _cfg(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path)


def _capture_run_lang(tmp_path: Path, payload: dict) -> str | None:
    """Create a run job and return the ``cfg.lang`` the runner received."""
    seen: list[str] = []

    async def fake_run_full(cfg, audio, **kw):
        seen.append(cfg.lang)
        return runner.RunResult(
            doc={}, srt="", json_path=tmp_path / "x.mai.json",
            srt_path=tmp_path / "x.srt", words=1, entries=1, dialogue=0)

    real = runner.run_full
    runner.run_full = fake_run_full
    try:
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"ID3")
        c = TestClient(create_app(_cfg(tmp_path)))
        r = c.post("/api/jobs", json={"kind": "run", "audio_path": str(audio),
                                      **payload})
        assert r.status_code == 200, r.text
        for _ in range(200):
            if seen:
                break
            time.sleep(0.01)
    finally:
        runner.run_full = real
    return seen[0] if seen else None


def test_job_pins_the_language_from_the_request(tmp_path):
    assert _capture_run_lang(tmp_path, {"lang": "en"}) == "en"


def test_job_falls_back_to_the_app_default(tmp_path):
    assert _capture_run_lang(tmp_path, {}) == "zh"


def test_loose_language_tags_are_normalized(tmp_path):
    assert _capture_run_lang(tmp_path, {"lang": "en-US"}) == "en"
