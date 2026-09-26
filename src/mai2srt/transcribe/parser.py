"""rich_transcription -> normalized document (words/utterances)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Word:
    text: str
    start: float
    end: float
    speaker: str | None = None   # inherited from the utterance, 1-based str


@dataclass
class Utterance:
    speaker: str | None
    start: float
    end: float
    text: str
    language: str | None = None


@dataclass
class Transcript:
    words: list[Word] = field(default_factory=list)
    utterances: list[Utterance] = field(default_factory=list)
    duration_s: float = 0.0
    language: str | None = None
    #: transcription-time source audio path (only set when loaded back from
    #: a .mai.json; parse_rich has no path to give)
    source: str | None = None

    def text(self) -> str:
        return "\n".join(u.text for u in self.utterances)


def parse_rich(rt: dict) -> Transcript:
    """Normalize one chunk's rich_transcription.

    Words missing text/start/end are skipped; speaker comes from the
    utterance (playground puts it there, 1-based int).
    """
    t = Transcript(duration_s=float(rt.get("durationSec") or 0.0))
    langs: list[str] = []
    for u in rt.get("utterances") or []:
        sp_raw = u.get("speaker")
        speaker = str(sp_raw) if sp_raw is not None else None
        lang = u.get("language")
        if lang:
            langs.append(lang)
        try:
            start = float(u["start"])
            end = float(u["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(u.get("text") or "")
        t.utterances.append(Utterance(speaker, start, end, text, lang))
        for w in u.get("words") or []:
            wtext = str(w.get("text") or w.get("word") or "").strip()
            if not wtext:
                continue
            try:
                ws = float(w["start"])
                we = float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            t.words.append(Word(wtext, ws, we, speaker))
    t.language = max(set(langs), key=langs.count) if langs else None
    return t


def has_word_timestamps(t: Transcript) -> bool:
    return len(t.words) > 0
