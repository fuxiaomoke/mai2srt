"""Load back the .mai.json files the CLI exports (heal-jimaku-style reuse)."""
from __future__ import annotations

import json
from pathlib import Path

from .parser import Transcript, Utterance, Word


def load_mai_json(path: str | Path) -> Transcript:
    """Rebuild a Transcript from a ``<name>.mai.json`` export."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    t = Transcript(
        duration_s=float(data.get("duration") or 0.0),
        language=data.get("language"),
        source=data.get("source") or None,
    )
    for w in data.get("words") or []:
        text = str(w.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(w["start"])
            end = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        t.words.append(Word(text, start, end, w.get("speaker")))
    for u in data.get("utterances") or []:
        t.utterances.append(Utterance(
            u.get("speaker"),
            float(u.get("start") or 0.0),
            float(u.get("end") or 0.0),
            str(u.get("text") or ""),
            u.get("language"),
        ))
    return t
