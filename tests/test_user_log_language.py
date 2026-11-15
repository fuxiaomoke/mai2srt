"""The user log speaks ONE language -- the one the interface is set to.

The Transcribe page used to show English technical lines with Chinese
sentences dropped between them ("source: ..." / "压缩为 mp3 后上传" /
"upload plan: 1 file(s)"), which reads as broken rather than bilingual. The
log now follows the interface language, pinned per job; the DETAIL log and
the file log stay English, because whoever debugs the run reads those.

These tests pin the invariant rather than individual sentences: no Chinese in
an English run, no English-only leftovers in a Chinese run, and a catalogue
where every key carries both.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt import runner  # noqa: E402
from mai2srt.config import Config  # noqa: E402
from mai2srt.media import chunk as chunk_mod  # noqa: E402
from mai2srt.messages import TR_KEYS, normalize, tr  # noqa: E402
from mai2srt.transcribe.client import UploadResult  # noqa: E402

#: CJK ideographs plus full-width punctuation ("：（）「」，")
_CJK = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")

RICH = {"durationSec": 60.0, "utterances": [{
    "speaker": 1, "start": 0.0, "end": 1.0, "text": "hi", "language": "en",
    "words": [{"text": "hi", "start": 0.0, "end": 1.0}]}]}


def test_every_message_exists_in_both_languages():
    missing = [k for k in TR_KEYS if not tr("zh", k) or not tr("en", k)
               or tr("zh", k) == k or tr("en", k) == k]
    assert missing == []


def test_english_copy_contains_no_chinese():
    for key in TR_KEYS:
        en = tr("en", key, n=1, i=1, span="1 min", size=1.0, name="a.mp3",
                dur=1.0, ch=2, codec="mp3", err="e", path="p", words=1,
                dialogue=1, model="m", url="u", proto="p", effort="e",
                status=200, jurisdiction="US", utts=1, tool="ffmpeg", rc=1,
                before=1, after=1, cut=1)
        assert not _CJK.search(en), (key, en)
        # every placeholder resolved: a stray "{" means a key whose params the
        # raise site does not actually pass
        assert "{" not in en, (key, en)


def test_normalize_accepts_loose_language_tags():
    assert normalize("en") == "en"
    assert normalize("en-US") == "en"
    assert normalize("EN") == "en"
    assert normalize("zh-CN") == "zh"
    assert normalize(None) == "zh"
    assert normalize("") == "zh"


def test_missing_key_degrades_to_the_key_not_an_exception():
    assert tr("en", "no.such.key") == "no.such.key"
    assert tr("zh", "media.plan") == tr("zh", "media.plan")


# ------------------------------------------------------- the media progress

def _prepare_lines(monkeypatch, tmp_path, lang):
    monkeypatch.setattr(chunk_mod, "detect_quiet", lambda src, dur: chunk_mod.QuietMap(
        gaps=[(1329.0, 1330.0), (2669.0, 2670.0)],
        peak_db=-1.0, threshold_db=-31.0, duration_s=dur))
    monkeypatch.setattr(chunk_mod, "cut_chunk",
                        lambda src, dst, s, e: dst.write_bytes(b"x" * 2048) or dst)
    info = chunk_mod.MediaInfo(duration_s=4000.0, channels=2, sample_rate=44100,
                               codec="aac", ext="mp4", size_bytes=10**9)
    lines: list[str] = []
    chunk_mod.prepare(Path("lec.mp4"), info, tmp_path, lines.append, lang)
    return lines


def test_prepare_progress_is_chinese_only(monkeypatch, tmp_path):
    lines = _prepare_lines(monkeypatch, tmp_path, "zh")
    assert any("切块" in m for m in lines)
    for line in lines:
        assert not re.search(r"[A-Za-z]{4,}", line.replace("playground", "")), line


def test_prepare_progress_is_english_only(monkeypatch, tmp_path):
    lines = _prepare_lines(monkeypatch, tmp_path, "en")
    assert any("pieces" in m for m in lines)
    for line in lines:
        assert not _CJK.search(line), line


# --------------------------------------------------------- the job log lines

def _upload_lines(lang):
    pieces = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 30.0)]

    async def fake_upload(page, upath, mime, **k):
        if upath.name == "b.mp3":
            return UploadResult(rich={}, conversation_id="", request_id=None,
                                cleanup_ok=True, no_speech=True)
        return UploadResult(rich=RICH, conversation_id="c", request_id="r",
                            cleanup_ok=False)

    runner.transcribe_file, saved = fake_upload, runner.transcribe_file
    try:
        logs: list[str] = []
        asyncio.run(runner._upload_pieces(
            None, pieces, audio=Path("src.mp3"), diarize=True, keep=False,
            on_stage=lambda s, d: None, on_log=logs.append, lang=lang))
        return logs
    finally:
        runner.transcribe_file = saved


def test_job_lines_are_chinese_only():
    logs = _upload_lines("zh")
    assert any("没有人声" in m for m in logs)      # silent piece
    assert any("清理" in m for m in logs)          # cleanup failure
    assert any("完成" in m for m in logs)          # piece done
    for line in logs:
        assert not re.search(r"\b(chunk|utts|words|piece)\b", line), line


def test_job_lines_are_english_only():
    logs = _upload_lines("en")
    assert any("no speech" in m for m in logs)
    assert any("cleanup failed" in m for m in logs)
    assert any("done" in m for m in logs)
    for line in logs:
        assert not _CJK.search(line), line


def test_the_language_comes_from_the_config():
    cfg = Config(lang="en")
    assert cfg.lang == "en"
    assert Config().lang == "zh"


def test_no_speech_failure_speaks_the_configured_language():
    class _Stitched:
        transcript = type("T", (), {"words": []})()

    for lang, needle in (("zh", "没有检测到人声"), ("en", "No speech detected")):
        logs: list[str] = []
        with pytest.raises(runner.TranscribeError) as ei:
            runner._require_speech(_Stitched(), logs.append, lang)
        assert needle in str(ei.value)
        assert needle in logs[0]
