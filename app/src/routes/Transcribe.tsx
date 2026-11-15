import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  AudioLines, Check, ClipboardCopy, FileAudio, FileJson, FileVideo,
  FolderOpen, History, RotateCcw, ScrollText, Scissors, SlidersHorizontal,
  Upload, Wand2, X,
} from 'lucide-react';
import { open } from '@tauri-apps/plugin-dialog';
import { Button, Card, StatusDot, Toggle, useToast } from '../components/ui';
import { useI18n, type Key } from '../lib/i18n';
import {
  getSubtitleParams, getSystem, preflight, revealPath,
  warmParams, warmSystem, withBackendRetry,
  type PreflightKey, type SubtitleParams, type SystemInfo,
} from '../lib/api';
import {
  isTerminal, jobBoot, jobCancel, jobDismiss, jobExpand, jobFail, jobLeave,
  jobMarkSeen, jobPickFile, jobStart, useJobSession,
} from '../lib/jobSession';
import { loadUseLlm, storeUseLlm } from '../lib/subtitle';

/** how a missing prerequisite is named in the refusal toast */
const PREFLIGHT_LABEL: Record<PreflightKey, Key> = {
  ffmpeg: 't.pre.ffmpeg',
  browser: 't.pre.browser',
  session: 't.pre.session',
  llm: 't.pre.llm',
};

const STAGES: { id: string; label: Key; icon: typeof FileAudio }[] = [
  { id: 'prepare', label: 't.stage.prepare', icon: FileAudio },
  { id: 'upload', label: 't.stage.upload', icon: Upload },
  { id: 'transcribe', label: 't.stage.transcribe', icon: AudioLines },
  { id: 'segment', label: 't.stage.segment', icon: Scissors },
  { id: 'postprocess', label: 't.stage.postprocess', icon: Wand2 },
];

/** accepted drop/pick inputs (user-approved curated lists; the backend
 *  pipeline itself is container-agnostic -- probe reads the audio stream
 *  and -vn encode strips the video track) */
const AUDIO_EXTS = ['mp3', 'wav', 'm4a', 'flac', 'ogg', 'opus', 'aac', 'wma'];
const VIDEO_EXTS = ['mp4', 'mkv', 'webm', 'mov', 'm4v', 'avi', 'wmv', 'ts', 'flv'];

function classify(p: string): 'json' | 'video' | 'audio' | null {
  const low = p.toLowerCase();
  if (low.endsWith('.mai.json') || low.endsWith('.json')) return 'json';
  if (VIDEO_EXTS.some((e) => low.endsWith('.' + e))) return 'video';
  if (AUDIO_EXTS.some((e) => low.endsWith('.' + e))) return 'audio';
  return null;
}

function isVideoPath(p: string): boolean {
  return VIDEO_EXTS.some((e) => p.toLowerCase().endsWith('.' + e));
}

/** The transcribe page is a VIEW of `jobSession` (see that module's header for
 *  why the run lives outside the page). Everything that used to be component
 *  state -- job id, stage, both log streams, result, error, the picked file --
 *  now comes from the store, so navigating away and back no longer destroys it;
 *  only genuinely view-local things (the detail toggle, the "copied" flash,
 *  drag hover) stay here and are expected to reset. */
export function TranscribePage() {
  const { t, tf, lang } = useI18n();
  const job = useJobSession((s) => s);
  // seeded from the startup warm cache: on a hit the status line and the
  // params card are complete on the first frame (their reserved/invisible
  // slots below cover only the cold-cache case)
  const [sys, setSys] = useState<SystemInfo | null>(warmSystem);
  // shared with the refine workspace (persisted) so both pages agree
  const [useLlm, setUseLlmState] = useState(loadUseLlm);
  const [params, setParams] = useState<SubtitleParams | null>(warmParams);
  // true from the click until the job exists: instant feedback even before
  // the createJob roundtrip resolves (the click must never feel dead)
  const [starting, setStarting] = useState(false);
  const [showDetail, setShowDetail] = useState(false);
  const [copied, setCopied] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const toast = useToast();
  const logRef = useRef<HTMLDivElement>(null);

  const running = job.phase === 'running';
  const busy = starting || running;
  /** a finished run the user has already looked at, seen from a later visit:
   *  the screen shows the invitation plus one thin "上次…" bar instead of the
   *  old log wall. Nothing is deleted -- 查看 reopens the exact same screen. */
  const folded = job.collapsed && isTerminal(job.phase);
  /** the file the page is about. Folded means "this session is history", so the
   *  drop zone is empty again -- and `start()` follows the same value, which is
   *  what keeps an invisible file from being re-transcribed. */
  const shownPath = folded ? '' : job.path;
  const shownIsJson = !folded && job.isJson;

  const setUseLlm = (v: boolean) => {
    setUseLlmState(v);
    storeUseLlm(v);
  };

  // mount: restore a run this document never saw (webview reload, renderer
  // crash -- the backend job keeps running through both) and refresh the two
  // payloads the page paints
  useEffect(() => {
    jobBoot();
    // retry-wrapped: this page mounts BEFORE the startup prefetch settles, so
    // its own first fetch lands while the packaged sidecar may still be
    // booting. A one-shot fetch then failed once and left the status line and
    // the params card invisible for good (they only reappear after the user
    // switches pages and the warm cache happens to be filled by then).
    let alive = true;
    withBackendRetry(() => getSystem())
      .then((s) => { if (alive) setSys(s); })
      .catch(() => { if (alive) toast('warn', t('t.toast.backendDown')); });
    withBackendRetry(() => getSubtitleParams())
      .then(({ params: p }) => { if (alive) setParams(p); })
      .catch(() => { /* the status-line toast above already said it */ });
    return () => { alive = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // rule 2, first half: a terminal run has been PAINTED, so it has been seen.
  // Only then may leaving fold it (jobLeave) -- a run that finished while the
  // page was closed is laid out in full the first time it is opened.
  useEffect(() => {
    if (isTerminal(job.phase)) jobMarkSeen();
  }, [job.phase]);

  // rule 2, second half: folding happens on the way OUT, never on arrival, so
  // the screen never reflows under the user's eyes. Unmount-safe by
  // construction: jobLeave reads the live module state, not this render.
  useEffect(() => () => jobLeave(), []);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [job.logs, job.detailLogs, showDetail]);

  // native drag-drop with absolute paths (tauri)
  useEffect(() => {
    let un: (() => void) | undefined;
    (async () => {
      try {
        const { getCurrentWebview } = await import('@tauri-apps/api/webview');
        un = await getCurrentWebview().listen<{ paths: string[] }>(
          'tauri://drag-drop',
          (e) => {
            const p = e.payload.paths?.[0];
            if (!p) return;
            if (classify(p) === null) {
              toast('warn', t('t.drop.rejected'));
              return;
            }
            jobPickFile(p, p.endsWith('.mai.json'));
          },
        );
      } catch { /* plain browser */ }
    })();
    return () => un?.();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function pick() {
    const p = await open({
      multiple: false,
      filters: job.isJson
        ? [{ name: 'mai2srt json', extensions: ['json'] }]
        : [
            { name: 'audio', extensions: AUDIO_EXTS },
            { name: 'video', extensions: VIDEO_EXTS },
          ],
    });
    if (typeof p === 'string') jobPickFile(p, p.endsWith('.mai.json'));
  }

  async function start() {
    if (busy || !shownPath) return;
    // busy FIRST (a double click must not fire two preflights), but the store
    // is only reset once the job is really going to start: a refused click
    // must not wipe the previous run's result and log
    setStarting(true);
    try {
      // refuse up front rather than watch the job die a minute later: the
      // backend names exactly which prerequisite is missing (static check,
      // instant -- an expired session is still caught inside the job)
      const kind = job.isJson ? 'process' : 'run';
      const pf = await preflight(kind, useLlm).catch(() => null);
      if (pf && !pf.ok) {
        toast('warn', tf('t.toast.missing', {
          items: pf.missing.map((k) => t(PREFLIGHT_LABEL[k])).join('、'),
        }));
        return;
      }
      await jobStart(job.isJson
        ? { kind: 'process', mai_json_path: shownPath, use_llm: useLlm, lang }
        : { kind: 'run', audio_path: shownPath, use_llm: useLlm, lang });
    } catch (e) {
      // stays in the store, not in this component: an error that vanishes
      // because the user changed pages is the bug this refactor is about
      jobFail(String(e));
    } finally {
      setStarting(false);
    }
  }

  // -1 while `starting`: the job does not exist yet, so the store still holds
  // the PREVIOUS run's stage. Lighting that stage up would claim the new run
  // had already reached it.
  const stageIdx = running ? STAGES.findIndex((s) => s.id === job.stage) : -1;
  // center the invitation until real work content appears, then anchor top.
  // A folded run is content-free BY DESIGN (the bar is one line), so the page
  // goes back to looking like a fresh one.
  const engaged = busy || (!folded
    && (job.logs.length > 0 || !!job.result || !!job.error
        || job.phase === 'cancelled'));

  /* One click, one pasteable report: the version/system header a maintainer
     always has to ask for, plus the diagnostic stream. Falls back to the
     deprecated execCommand path because a packaged WebView may refuse the
     clipboard API (no user-gesture permission model there). */
  async function copyDiagnostics() {
    const text = [
      `mai2srt v${sys?.version ?? '?'} · python ${sys?.python ?? '?'}`,
      `ffmpeg: ${sys?.ffmpeg.found ? (sys.ffmpeg.source ?? 'found') : 'MISSING'}`
        + ` · browser: ${sys?.chrome.channel ?? 'none'}`,
      '----------------------------------------',
      ...(job.detailLogs.length ? job.detailLogs : job.logs),
    ].join('\n');
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

  /** "上次：a.mp3 · 已完成 · 467 条字幕" -- the one-line stand-in for a finished
   *  run. 查看 reopens the very same screen (nothing was ever thrown away). */
  const restoreStatus = job.phase === 'done'
    ? t('t.result.done')
    : job.phase === 'error' ? t('t.restore.failed') : t('t.toast.cancelled');
  const restoreEntries = job.result?.entries;

  return (
    <div
      className={`mx-auto flex min-h-full w-full max-w-[min(1200px,92%)] flex-col gap-5 px-8 py-8 ${
        engaged ? 'justify-start' : 'justify-center'
      }`}
    >
      {/* system status line: ALWAYS occupies its row (invisible until sys
          lands). The column is justify-center while nothing is engaged,
          so a block that appears late would shove the drop zone up by
          half its height -- reserving the slot keeps the page still */}
      <div
        className={`flex flex-wrap items-center gap-4 text-[12px] text-ink-2 ${
          sys ? '' : 'invisible'
        }`}
      >
        <span className="flex items-center gap-1.5">
          <StatusDot ok={!!sys?.ffmpeg.found} /> ffmpeg
        </span>
        <span className="flex items-center gap-1.5">
          <StatusDot ok={!!sys?.chrome.found} /> {sys?.chrome.channel ?? t('t.sys.noBrowser')}
        </span>
        <span className="flex items-center gap-1.5">
          <StatusDot ok={!!sys?.cookie_jar} /> {t('t.sys.session')}
        </span>
        <span className="ml-auto font-mono text-[11px] text-ink-3">
          {sys ? `v${sys.version}` : ''}
        </span>
      </div>

      {/* drop zone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          const f = e.dataTransfer.files[0];
          if (!f) return;
          if (classify(f.name) === null) {
            toast('warn', t('t.drop.rejected'));
            return;
          }
          // a drop is a new task (rule 1): the previous run's trace goes away
          jobPickFile(f.name, f.name.endsWith('.mai.json'));
        }}
        className="relative"
      >
        <Card
          className={`cursor-pointer p-10 text-center transition-all duration-(--dur-in) ${
            dragOver ? 'scale-[1.01] border-primary/60' : ''
          }`}
        >
          <div onClick={pick} className="flex min-h-[240px] flex-col items-center justify-center gap-3">
            <motion.div
              animate={dragOver ? { y: -4, scale: 1.06 } : { y: 0, scale: 1 }}
              transition={{ duration: 0.2, ease: [0.34, 1.56, 0.64, 1] }}
              className="grid h-14 w-14 place-items-center rounded-(--radius-l) bg-primary-dim text-primary"
            >
              {shownIsJson ? <FileJson size={26} strokeWidth={1.8} />
                : isVideoPath(shownPath) ? <FileVideo size={26} strokeWidth={1.8} />
                : <FileAudio size={26} strokeWidth={1.8} />}
            </motion.div>
            {shownPath ? (
              <>
                <p className="text-[15px] font-semibold text-ink-1">
                  {shownPath.split(/[\\/]/).pop()}
                </p>
                <p className="font-mono text-[11px] text-ink-3">{shownPath}</p>
              </>
            ) : (
              <p className="text-[14px] text-ink-2">
                {t('t.drop.hint')}
              </p>
            )}
          </div>
        </Card>
      </div>

      {/* persisted subtitle params (all 8, edited on the refine page): sits
          ABOVE the action row so it reads as "what will this run use"
          before the eye reaches the start button. Like the status line it
          is ALWAYS in the flow (invisible until loaded) -- a card that
          appeared late would shift the centered column */}
      <Card className={`flex items-center gap-3 px-4 py-2.5 ${params ? '' : 'invisible'}`}>
        <span className="grid h-7 w-7 shrink-0 place-items-center rounded-(--radius-s) bg-primary-dim text-primary">
          <SlidersHorizontal size={13} strokeWidth={2} />
        </span>
        <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">
          {params && (
            <>
              {tf('t.params.summary', {
                dur: params.max_duration,
                chars: params.max_chars,
                minDur: params.min_duration,
                minCh: params.min_chars,
                pause: params.split_pause,
                merge: params.merge_gap,
                tol: params.tolerance,
                exp: params.expand,
              })}
              <span className="mx-2 text-ink-3">|</span>
              <span className={useLlm ? 'font-medium text-primary' : 'text-ink-3'}>
                {useLlm ? t('t.params.llmOn') : t('t.params.llmOff')}
              </span>
            </>
          )}
        </span>
        {/* custom (not Button ghost): border+bg+text all shift to the theme
            color on hover and the press scales down, so the click is felt */}
        <button
          onClick={() => window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail: 'refine' }))}
          className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-(--radius-s) border border-line-1 px-4 text-[13px] font-semibold text-ink-1 transition-all duration-(--dur-in) hover:border-primary/60 hover:bg-primary-dim hover:text-primary active:scale-[0.97]"
        >
          <SlidersHorizontal size={13} strokeWidth={2} /> {t('t.params.adjust')}
        </button>
      </Card>

      {/* controls: actions left, options right so the row uses its width */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <Button onClick={start} busy={busy} disabled={!shownPath || busy}>
            {busy ? t('t.action.running') : job.isJson ? t('t.action.process') : t('t.action.run')}
          </Button>
          {running && (
            <Button variant="danger" onClick={jobCancel}>
              {t('t.action.cancel')}
            </Button>
          )}
        </div>
        <Toggle checked={useLlm} onChange={setUseLlm} label={t('t.toggle.llm')} />
      </div>

      {/* stage timeline: appears the moment the click lands (busy), even
          though the first stage event may still be in flight. The running
          file's NAME lives here because the drop zone may already show the
          next file (a drop while a job runs only prepares it) */}
      <AnimatePresence>
        {busy && (
          /* height-only reveal: a glass Card lives inside, and opacity
             animation breaks backdrop-filter sampling mid-fade (the
             glass flash bug) -- the card would look flat gray until the
             fade lands and the tint pops in late */
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: 'auto' }}
            exit={{ height: 0 }}
            className="overflow-hidden"
          >
            <Card className="flex flex-col gap-3 px-6 py-4">
              {/* only while the job EXISTS: during the createJob roundtrip the
                  store still holds the previous run's title */}
              {running && job.title && (
                <p className="truncate text-center font-mono text-[11px] text-ink-3">
                  {tf('t.job.runningFile', { name: job.title })}
                </p>
              )}
              <div className="flex items-center justify-between">
                {STAGES.map((s, i) => {
                  const on = i <= stageIdx;
                  const current = i === stageIdx;
                  return (
                    <div key={s.id} className="flex flex-col items-center gap-2">
                      <motion.div
                        animate={current ? { scale: [1, 1.15, 1] } : { scale: 1 }}
                        transition={current ? { repeat: Infinity, duration: 1.4 } : {}}
                        className={`grid h-9 w-9 place-items-center rounded-full border transition-colors duration-(--dur-in) ${
                          on
                            ? 'border-primary bg-primary-dim text-primary'
                            : 'border-line-1 text-ink-3'
                        }`}
                      >
                        <s.icon size={15} strokeWidth={2} />
                      </motion.div>
                      <span className={`text-[10.5px] font-medium ${on ? 'text-primary' : 'text-ink-3'}`}>
                        {t(s.label)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </Card>
          </motion.div>
        )}
      </AnimatePresence>

      {/* logs: the plain lines by default, the diagnostic stream (which always
          runs, whichever view is open) behind the detail button -- so a bug
          report can carry real numbers without pushing dB figures at everyone */}
      {!folded && (job.logs.length > 0 || job.detailLogs.length > 0) && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="ghost"
              onClick={() => setShowDetail((v) => !v)}
              disabled={job.detailLogs.length === 0}
              title={t('t.log.detailHint')}
            >
              <ScrollText size={13} strokeWidth={2} />
              {showDetail
                ? t('t.log.hideDetail')
                : tf('t.log.showDetail', { n: job.detailLogs.length })}
            </Button>
            <Button variant="ghost" onClick={copyDiagnostics} title={t('t.log.copyHint')}>
              {copied
                ? <Check size={13} strokeWidth={2} />
                : <ClipboardCopy size={13} strokeWidth={2} />}
              {copied ? t('t.log.copied') : t('t.log.copy')}
            </Button>
            {/* rule 3 while the run is on screen: the bar's × (below) only
                exists in the folded view, so the expanded view needs its own */}
            {isTerminal(job.phase) && (
              <Button variant="ghost" onClick={jobDismiss} title={t('t.restore.clear')}>
                <X size={13} strokeWidth={2} />
                {t('t.restore.clear')}
              </Button>
            )}
          </div>
          <div
            ref={logRef}
            className="glass rounded-(--radius-m) h-56 overflow-y-auto px-4 py-3 font-mono text-[11.5px] leading-[1.7] text-ink-2"
          >
            {(showDetail ? job.detailLogs : job.logs).map((l, i) => (
              <div key={i} className={l.startsWith('dialogue') ? 'text-primary' : ''}>
                {l}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* result */}
      {!folded && job.result && (
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}>
          <Card className="flex flex-col gap-3 p-5">
            <div className="flex items-center justify-between">
              <span className="text-[14px] font-semibold text-ok">{t('t.result.done')}</span>
              <span className="text-[12px] text-ink-2">
                {job.result.words != null && `${job.result.words} ${t('t.result.words')} / `}
                {job.result.entries} {t('t.result.entries')}
                {job.result.dialogue != null && ` / ${job.result.dialogue} ${t('t.result.dialogue')}`}
              </span>
            </div>
            {(['srt_path', 'json_path'] as const).map(
              (k) =>
                job.result?.[k] && (
                  <div key={k} className="flex items-center gap-3">
                    <span className="w-10 text-[11px] uppercase text-ink-3">
                      {k === 'srt_path' ? 'srt' : 'json'}
                    </span>
                    <code className="flex-1 truncate font-mono text-[11.5px] text-primary">
                      {job.result[k]}
                    </code>
                    <button
                      className="text-ink-2 transition-colors hover:text-primary"
                      onClick={() =>
                        job.result?.[k] &&
                        revealPath(job.result[k]!).catch((e) =>
                          toast('error', String(e).slice(0, 160)),
                        )
                      }
                      title={t('t.hint.reveal')}
                    >
                      <FolderOpen size={15} strokeWidth={2} />
                    </button>
                  </div>
                ),
            )}
          </Card>
        </motion.div>
      )}

      {/* one-click hand-off: refine auto-loads this transcription and
          restores its LLM segmentation when the run produced one.
          Styled as a primary action (same colors/hover/shadow as 开始转录)
          but keeps its own size; flush left, not centered */}
      {!folded && job.result && (
        <div>
          <button
            onClick={() => window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail: 'refine' }))}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-(--radius-s) bg-primary px-5 text-[13px] font-semibold text-on-primary shadow-[var(--shadow-1)] transition-all duration-(--dur-in) hover:bg-primary-hover active:brightness-95"
          >
            <SlidersHorizontal size={13} strokeWidth={2} /> {t('t.result.toRefine')}
          </button>
        </div>
      )}

      {!folded && job.phase === 'error' && job.error && (
        <Card className="border-err/40 p-4">
          <p className="break-words font-mono text-[12px] text-err">{job.error}</p>
        </Card>
      )}

      {/* cancelled is an outcome, not a failure: same neutral card, no red */}
      {!folded && job.phase === 'cancelled' && (
        <Card className="p-4">
          <p className="text-[12px] text-ink-2">{t('t.toast.cancelled')}</p>
        </Card>
      )}

      {/* the folded stand-in for a finished run (rule 2). Built from the SAME
          Card anatomy as the params card above -- icon chip, one text line,
          bordered action -- so the page reads as one material instead of a
          flat outlined row sitting between glass cards (user feedback
          2026-11-16). 查看 is a pure view toggle: the store still holds every
          line, so reopening cannot lose anything; × is rule 3 */}
      {folded && (
        <Card className="flex items-center gap-3 px-4 py-2.5">
          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-(--radius-s) bg-primary-dim text-primary">
            <History size={13} strokeWidth={2} />
          </span>
          <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">
            <span className="text-ink-3">{t('t.restore.last')}：</span>
            <span className="text-ink-1">{job.title}</span>
            <span className="mx-2 text-ink-3">|</span>
            <span className={job.phase === 'done' ? 'text-ok' : 'text-ink-3'}>
              {restoreStatus}
            </span>
            {restoreEntries != null && (
              <>
                <span className="mx-1.5 text-ink-3">·</span>
                {restoreEntries} {t('t.result.entries')}
              </>
            )}
          </span>
          <button
            onClick={jobExpand}
            className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-(--radius-s) border border-line-1 px-4 text-[13px] font-semibold text-ink-1 transition-all duration-(--dur-in) hover:border-primary/60 hover:bg-primary-dim hover:text-primary active:scale-[0.97]"
          >
            <RotateCcw size={13} strokeWidth={2} /> {t('t.restore.view')}
          </button>
          <button
            onClick={jobDismiss}
            title={t('t.restore.clear')}
            className="grid h-8 w-8 shrink-0 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-ink-1"
          >
            <X size={13} strokeWidth={2} />
          </button>
        </Card>
      )}
    </div>
  );
}
