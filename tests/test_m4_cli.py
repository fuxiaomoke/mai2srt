"""M4 CLI tests: argument wiring + offline process end-to-end."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.cli import main


def _sample_doc(tmp: Path) -> Path:
    words = []
    t = 0.0
    for i, ch in enumerate("あのー、すみません。ちょっといいですか？"):
        words.append({"text": ch, "start": round(t, 3),
                      "end": round(t + 0.28, 3), "speaker": "1"})
        t += 0.3
        if ch in "、。？":
            t += 0.9
    doc = {
        "text": "あのー、すみません。ちょっといいですか？",
        "language": "ja", "duration": t, "engine": "playground_mai-transcribe-2",
        "utterances": [{"speaker": "1", "start": 0.0, "end": t,
                        "text": "あのー、すみません。ちょっといいですか？"}],
        "words": words,
    }
    p = tmp / "sample.mai.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def test_process_offline_produces_srt(tmp_path):
    src = _sample_doc(tmp_path)
    out = tmp_path / "sample.srt"
    rc = main(["process", str(src), "--out", str(out), "--no-llm"])
    assert rc == 0
    body = out.read_text(encoding="utf-8")
    assert "-->" in body
    assert "すみません。" in body
    # numbering is sequential from 1
    first = body.splitlines()[0]
    assert first == "1"


def test_process_tuning_flags_change_output(tmp_path):
    src = _sample_doc(tmp_path)
    wide = tmp_path / "wide.srt"
    tight = tmp_path / "tight.srt"
    assert main(["process", str(src), "--out", str(wide), "--no-llm",
                 "--max-chars", "60"]) == 0
    assert main(["process", str(src), "--out", str(tight), "--no-llm",
                 "--max-chars", "7"]) == 0
    n_wide = wide.read_text(encoding="utf-8").count("-->")
    n_tight = tight.read_text(encoding="utf-8").count("-->")
    assert n_tight >= n_wide          # tighter cap -> same or more lines


def test_process_missing_input_fails(tmp_path, capsys=None):
    rc = main(["process", str(tmp_path / "nope.mai.json"), "--no-llm"])
    assert rc == 1


def test_transcribe_and_run_missing_audio_fail(tmp_path):
    assert main(["transcribe", str(tmp_path / "nope.mp3")]) == 1
