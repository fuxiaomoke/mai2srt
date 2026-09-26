"""mai2srt command line: login / transcribe / process / run.

    mai2srt login
    mai2srt transcribe <audio> [--out out.mai.json]
                        [--keep-conversation] [--include-raw]
    mai2srt process <x.mai.json> [--out out.srt] [--no-llm] [tuning flags]
    mai2srt run <audio> [--out out.srt] [--json out.mai.json] [--no-llm]
              [tuning flags]

Speaker diarization cannot be disabled: the playground server ignores
{"diarize": false} entirely (live-verified 2026-09-26 -- an empty
transcribeOptions AND an explicit false both came back diarized), so
the old --no-diarize flag was a no-op and has been removed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import Config
from .media.probe import MediaError
from .runner import (
    build_document,
    finish_srt,
    login_flow,
    run_full,
    transcribe_audio,
)
from .transcribe.client import TranscribeError
from .transcribe.persist import load_mai_json

log = logging.getLogger("mai2srt")


def _setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.ensure_dirs()
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                            "%H:%M:%S")
    root = logging.getLogger("mai2srt")
    root.setLevel(level)
    root.handlers.clear()
    con = logging.StreamHandler(sys.stderr)
    con.setFormatter(fmt)
    root.addHandler(con)
    fh = logging.FileHandler(cfg.log_dir / "mai2srt.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


async def cmd_login(cfg: Config, account: str | None = None) -> int:
    if account:
        from .config import add_account, set_active_account
        name = account.strip()
        try:
            add_account(cfg, name)
        except ValueError:
            pass  # already registered (or default): just (re)sign in
        set_active_account(cfg, name)
        log.info("signing in account '%s'", name)
    try:
        await login_flow(cfg, on_log=log.info)
    except TranscribeError as e:
        log.error("%s", e)
        return 1
    log.info("signed in; session persisted under %s", cfg.data_dir)
    return 0


def _resolve_keep(cfg: Config, args: argparse.Namespace) -> bool:
    """--keep-conversation overrides the app-config toggle; when the flag is
    absent the toggle (default ON = delete) decides, so CLI and GUI agree."""
    if args.keep_conversation:
        return True
    from .config import auto_delete_conversation
    return not auto_delete_conversation(cfg)


async def cmd_transcribe(cfg: Config, args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    doc, stitched = await transcribe_audio(
        cfg, audio,
        keep=_resolve_keep(cfg, args),
        include_raw=args.include_raw,
        on_log=log.info)
    out_path = Path(args.out) if args.out else audio.with_suffix(".mai.json")
    _write_doc(out_path, doc)
    log.info("wrote %s  (%d words, %d utterances, %.1fs)",
             out_path, len(stitched.transcript.words),
             len(stitched.transcript.utterances), stitched.audio_duration_s)
    return 0


def _write_doc(out_path: Path, doc: dict) -> None:
    out_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# segmentation + post-processing (M2/M3) -- thin wrappers over runner
# ---------------------------------------------------------------------------

def _tuned_params(args: argparse.Namespace) -> tuple:
    from .segment import SegmentParams
    from .postprocess import PostprocessParams
    sp = SegmentParams(
        max_duration_s=args.max_duration,
        max_chars=args.max_chars,
        min_duration_s=args.min_duration,
        min_chars=args.min_chars,
        split_pause_s=args.split_pause,
    )
    pp = PostprocessParams(
        max_duration_s=args.max_duration,
        max_chars=args.max_chars,
        min_duration_s=args.min_duration,
        merge_gap_threshold_s=args.merge_gap,
        dialogue_gap_tolerance_s=args.tolerance,
        mai_expand_s=args.expand,
    )
    return sp, pp


def cmd_process(cfg: Config, args: argparse.Namespace) -> int:
    src = Path(args.mai_json)
    if not src.exists():
        log.error("input not found: %s", src)
        return 1
    t = load_mai_json(src)
    if not t.words:
        log.error("no words in %s; re-transcribe the source audio", src)
        return 1
    log.info("loaded %s: %d words, %.1fs", src.name, len(t.words), t.duration_s)
    sp, pp = _tuned_params(args)
    srt, n_entries, n_dial = finish_srt(
        cfg, t, t.duration_s, segment_params=sp, post_params=pp,
        use_llm=not args.no_llm, on_log=log.info)

    out = Path(args.out) if args.out else src.with_suffix(".srt")
    out.write_text(srt, encoding="utf-8")
    log.info("wrote %s  (%d entries, %d dialogue)", out, n_entries, n_dial)
    return 0


async def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    sp, pp = _tuned_params(args)
    res = await run_full(
        cfg, audio,
        keep=_resolve_keep(cfg, args),
        include_raw=args.include_raw,
        segment_params=sp, post_params=pp,
        use_llm=not args.no_llm,
        on_log=log.info)
    log.info("done: %d words -> %d entries (%d dialogue)",
             res.words, res.entries, res.dialogue)
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    import uvicorn
    from .server.app import create_app
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port,
                log_level="info")
    return 0


def _add_tuning(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--max-duration", type=float, default=12.0,
                    help="max line duration s (default 12)")
    sp.add_argument("--max-chars", type=int, default=60,
                    help="max line chars (default 60)")
    sp.add_argument("--min-duration", type=float, default=1.2,
                    help="min line duration s (default 1.2)")
    sp.add_argument("--min-chars", type=int, default=5,
                    help="min line chars, 0 disables (default 5)")
    sp.add_argument("--split-pause", type=float, default=0.8,
                    help="inter-word pause s that marks a split candidate (default 0.8)")
    sp.add_argument("--merge-gap", type=float, default=0.8,
                    help="max gap s the smart merge may bridge (default 0.8)")
    sp.add_argument("--tolerance", type=float, default=0.2,
                    help="dialogue clustering tolerance s (default 0.2)")
    sp.add_argument("--expand", type=float, default=0.25,
                    help="timestamp outward expansion s (default 0.25)")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stderr, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(
        prog="mai2srt",
        description="playground.microsoft.ai MAI-Transcribe-2 transcription "
                    "-> word-timestamp JSON -> segmented/dialogue SRT")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("login", help="open a browser and (re)establish the session")
    lp.add_argument("--account", default=None,
                    help="sign into this named account (created if new); "
                         "each account keeps its own cookies + browser profile")

    tp = sub.add_parser("transcribe", help="audio -> <name>.mai.json")
    tp.add_argument("audio")
    tp.add_argument("--out", default=None, help="output json path")
    tp.add_argument("--keep-conversation", action="store_true", default=None,
                    help="keep the playground conversation (overrides the "
                         "app config toggle; default = config value)")
    tp.add_argument("--include-raw", action="store_true",
                    help="embed raw rich_transcription payloads")

    pp = sub.add_parser("process", help="<name>.mai.json -> SRT (offline)")
    pp.add_argument("mai_json")
    pp.add_argument("--out", default=None, help="output srt path")
    pp.add_argument("--no-llm", action="store_true",
                    help="skip LLM split-point selection (deterministic only)")
    _add_tuning(pp)

    rp = sub.add_parser("run", help="audio -> transcribe + segment + SRT")
    rp.add_argument("audio")
    rp.add_argument("--out", default=None, help="output srt path")
    rp.add_argument("--json", default=None, help="output json path "
                    "(default <name>.mai.json next to the audio)")
    rp.add_argument("--keep-conversation", action="store_true", default=None)
    rp.add_argument("--include-raw", action="store_true")
    rp.add_argument("--no-llm", action="store_true",
                    help="skip LLM split-point selection (deterministic only)")
    _add_tuning(rp)

    sv = sub.add_parser("serve", help="run the UI backend API server")
    sv.add_argument("--port", type=int, default=47613)

    args = ap.parse_args(argv)
    cfg = Config()
    _setup_logging(cfg, args.verbose)

    if args.cmd == "login":
        return asyncio.run(cmd_login(cfg, account=args.account))
    if args.cmd == "transcribe":
        try:
            return asyncio.run(cmd_transcribe(cfg, args))
        except (TranscribeError, MediaError) as e:
            log.error("transcription failed: %s", e)
            return 1
    if args.cmd == "process":
        return cmd_process(cfg, args)
    if args.cmd == "run":
        try:
            return asyncio.run(cmd_run(cfg, args))
        except (TranscribeError, MediaError) as e:
            log.error("run failed: %s", e)
            return 1
    if args.cmd == "serve":
        return cmd_serve(cfg, args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
