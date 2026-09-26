import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  AudioLines, FileAudio, FileJson, FileVideo, FolderOpen, Scissors,
  SlidersHorizontal, Upload, Wand2,
} from 'lucide-react';
import { open } from '@tauri-apps/plugin-dialog';
import { Button, Card, StatusDot, Toggle, useToast } from '../components/ui';
import { useI18n, type Key } from '../lib/i18n';
import {
  cancelJob, createJob, getSubtitleParams, getSystem, revealPath, subscribeJob,
  warmParams, warmSystem,
  type DoneData, type SubtitleParams, type SystemInfo,
} from '../lib/api';
import { loadUseLlm, storeUseLlm } from '../lib/subtitle';

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

export function TranscribePage() {
  const { t, tf } = useI18n();
  // seeded from the startup warm cache: on a hit the status line and the
  // params card are complete on the first frame (their reserved/invisible
  // slots below cover only the cold-cache case)
  const [sys, setSys] = useState<SystemInfo | null>(warmSystem);
  const [path, setPath] = useState('');
  const [isJson, setIsJson] = useState(false);
  // shared with the refine workspace (persisted) so both pages agree
  const [useLlm, setUseLlmState] = useState(loadUseLlm);
  const [params, setParams] = useState<SubtitleParams | null>(warmParams);
  const [jobId, setJobId] = useState<string | null>(null);
  // true from the click until the job exists: instant feedback even before
  // the createJob roundtrip resolves (the click must never feel dead)
  const [starting, setStarting] = useState(false);
  const [stage, setStage] = useState('');
  const [logs, setLogs] = useState<string[]>([]);
  const [result, setResult] = useState<DoneData | null>(null);
  const [error, setError] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const toast = useToast();
  const logRef = useRef<HTMLDivElement>(null);
  const disposeRef = useRef<(() => void) | null>(null);

  const running = jobId !== null && !result && !error;
  const busy = starting || running;

  const setUseLlm = (v: boolean) => {
    setUseLlmState(v);
    storeUseLlm(v);
  };

  useEffect(() => {
    getSystem().then(setSys).catch(() => {});
    getSubtitleParams()
      .then(({ params: p }) => setParams(p))
      .catch(() => {});
    return () => disposeRef.current?.();
  }, []);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [logs]);

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
            setIsJson(p.endsWith('.mai.json'));
            setPath(p);
            setResult(null);
            setError('');
          },
        );
      } catch { /* plain browser */ }
    })();
    return () => un?.();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function pick() {
    const p = await open({
      multiple: false,
      filters: isJson
        ? [{ name: 'mai2srt json', extensions: ['json'] }]
        : [
            { name: 'audio', extensions: AUDIO_EXTS },
            { name: 'video', extensions: VIDEO_EXTS },
          ],
    });
    if (typeof p === 'string') {
      setPath(p);
      setResult(null);
      setError('');
    }
  }

  async function start() {
    if (busy) return;
    setStarting(true);
    setError('');
    setResult(null);
    setLogs([]);
    setStage('');
    try {
      const job = await createJob(
        isJson
          ? { kind: 'process', mai_json_path: path, use_llm: useLlm }
          : { kind: 'run', audio_path: path, use_llm: useLlm },
      );
      setJobId(job.id);
      disposeRef.current = subscribeJob(job.id, {
        onStage: (s) => setStage(s),
        onLog: (line) => setLogs((ls) => [...ls.slice(-500), line]),
        onDone: (d) => {
          setResult(d);
          toast('ok', t('t.toast.done'));
          getSystem().then(setSys).catch(() => {});
        },
        onError: (m) => {
          setError(m);
          toast('error', m.slice(0, 160));
        },
        onCancelled: () => setError(t('t.toast.cancelled')),
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setStarting(false);
    }
  }

  const stageIdx = STAGES.findIndex((s) => s.id === stage);
  // center the invitation until real work content appears, then anchor top
  const engaged = busy || logs.length > 0 || !!result || !!error;

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
          setIsJson(f.name.endsWith('.mai.json'));
          setPath(f.name);
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
              {isJson ? <FileJson size={26} strokeWidth={1.8} />
                : isVideoPath(path) ? <FileVideo size={26} strokeWidth={1.8} />
                : <FileAudio size={26} strokeWidth={1.8} />}
            </motion.div>
            {path ? (
              <>
                <p className="text-[15px] font-semibold text-ink-1">
                  {path.split(/[\\/]/).pop()}
                </p>
                <p className="font-mono text-[11px] text-ink-3">{path}</p>
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
          <Button onClick={start} busy={busy} disabled={!path || busy}>
            {busy ? t('t.action.running') : isJson ? t('t.action.process') : t('t.action.run')}
          </Button>
          {running && (
            <Button variant="danger" onClick={() => jobId && cancelJob(jobId).catch(() => {})}>
              {t('t.action.cancel')}
            </Button>
          )}
        </div>
        <Toggle checked={useLlm} onChange={setUseLlm} label={t('t.toggle.llm')} />
      </div>

      {/* stage timeline: appears the moment the click lands (busy), even
          though the first stage event may still be in flight */}
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
            <Card className="flex items-center justify-between px-6 py-4">
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
            </Card>
          </motion.div>
        )}
      </AnimatePresence>

      {/* logs */}
      {logs.length > 0 && (
        <div
          ref={logRef}
          className="glass rounded-(--radius-m) h-56 overflow-y-auto px-4 py-3 font-mono text-[11.5px] leading-[1.7] text-ink-2"
        >
          {logs.map((l, i) => (
            <div key={i} className={l.startsWith('dialogue') ? 'text-primary' : ''}>
              {l}
            </div>
          ))}
        </div>
      )}

      {/* result */}
      {result && (
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}>
          <Card className="flex flex-col gap-3 p-5">
            <div className="flex items-center justify-between">
              <span className="text-[14px] font-semibold text-ok">{t('t.result.done')}</span>
              <span className="text-[12px] text-ink-2">
                {result.words != null && `${result.words} ${t('t.result.words')} / `}
                {result.entries} {t('t.result.entries')}
                {result.dialogue != null && ` / ${result.dialogue} ${t('t.result.dialogue')}`}
              </span>
            </div>
            {(['srt_path', 'json_path'] as const).map(
              (k) =>
                result[k] && (
                  <div key={k} className="flex items-center gap-3">
                    <span className="w-10 text-[11px] uppercase text-ink-3">
                      {k === 'srt_path' ? 'srt' : 'json'}
                    </span>
                    <code className="flex-1 truncate font-mono text-[11.5px] text-primary">
                      {result[k]}
                    </code>
                    <button
                      className="text-ink-2 transition-colors hover:text-primary"
                      onClick={() =>
                        result[k] &&
                        revealPath(result[k]!).catch((e) =>
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
      {result && (
        <div>
          <button
            onClick={() => window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail: 'refine' }))}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-(--radius-s) bg-primary px-5 text-[13px] font-semibold text-on-primary shadow-[var(--shadow-1)] transition-all duration-(--dur-in) hover:bg-primary-hover active:brightness-95"
          >
            <SlidersHorizontal size={13} strokeWidth={2} /> {t('t.result.toRefine')}
          </button>
        </div>
      )}

      {error && (
        <Card className="border-err/40 p-4">
          <p className="break-words font-mono text-[12px] text-err">{error}</p>
        </Card>
      )}
    </div>
  );
}
