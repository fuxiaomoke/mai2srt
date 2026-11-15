/**
 * JobSession — the transcription run, lifted out of the page.
 *
 * WHY (2026-11-16). The transcribe page used to OWN the run: job id, stage,
 * both log streams, the result, the error and the picked file all lived in
 * component state, and the SSE subscription was torn down on unmount. The
 * backend job is a process-level thing that keeps running either way, so
 * switching pages did not stop the work -- it only blinded the UI: coming back
 * mounted an EMPTY page that no longer knew a job existed, could not re-attach
 * to it, and (worse) offered "start" again, which the server then refused with
 * a raw 409. A run that finished while the user was in Settings said nothing at
 * all.
 *
 * The fix is the pattern this app already uses for audio (see player.ts): the
 * state is a MODULE-LEVEL singleton and the page is only a view of it. The
 * subscription lives here, so it survives every navigation; a job that finishes
 * elsewhere still raises its toast; and a reloaded webview (dev F5, a crashed
 * renderer) re-attaches by asking the backend what is still running.
 *
 * PERSISTENCE MAKES RESET A DESIGN QUESTION. A page that always starts empty
 * resets itself for free; a page that remembers has to say when it is done.
 * Three rules, weakest to strongest (agreed 2026-11-16):
 *
 *  1. PICKING A FILE IS A NEW TASK (`jobPickFile`). Dropping or browsing for a
 *     file is the user saying "next"; the previous run's log, stage, result and
 *     error go away right there.
 *  2. FINISHED + SEEN + LEFT = FOLDED AWAY (`jobMarkSeen` / `jobLeave`). A
 *     terminal run stays on screen until the user has actually LOOKED at it
 *     (`seen`) and then walked away; coming back shows the clean page plus one
 *     thin "上次…" bar that reopens the same screen. A run that finished while
 *     the page was closed is NOT folded -- nobody has seen it yet, so it is laid
 *     out in full.
 *  3. THE × ON THAT BAR CLEARS EVERYTHING (`jobDismiss`).
 *
 * A RUNNING job is never folded and never cleared: the live screen is the point.
 */
import { useSyncExternalStore } from 'react';
import {
  cancelJob, createJob, listJobs, subscribeJob, withBackendRetry,
  type DoneData,
} from './api';

export type JobPhase = 'idle' | 'running' | 'done' | 'error' | 'cancelled';

const TERMINAL: readonly JobPhase[] = ['done', 'error', 'cancelled'];
export const isTerminal = (p: JobPhase): boolean => TERMINAL.includes(p);

/** what `jobStart` needs; mirrors the POST /api/jobs body the page used to send */
export interface StartBody {
  kind: 'run' | 'process';
  audio_path?: string;
  mai_json_path?: string;
  use_llm?: boolean;
  lang?: 'zh' | 'en';
}

/** log line caps: the panel is a fixed-height scroller and a 55-min run emits
 *  far more than anyone scrolls back through. The DETAIL stream keeps more
 *  because that is the one a bug report pastes. */
const LOG_CAP = 500;
const DETAIL_CAP = 1000;

export interface JobSessionState {
  // --- the run ---------------------------------------------------------
  /** null = nothing has run in this launch (or it was dismissed) */
  jobId: string | null;
  /** 'run' | 'process' | null -- decides whether the page offers 转录 or 生成 */
  kind: string | null;
  /** file name of the run, straight from the backend (not derived from path:
   *  after a reload the path is unknown but the backend still knows the title) */
  title: string;
  phase: JobPhase;
  stage: string;
  logs: string[];
  detailLogs: string[];
  result: DoneData | null;
  /** terminal failure message; the cancelled copy is localized by the view */
  error: string;

  // --- what the user picked for the NEXT run ---------------------------
  /** survives page switches; shown in the drop zone unless the run is folded */
  path: string;
  isJson: boolean;

  // --- the collapse state machine (rules 2 and 3 above) ----------------
  /** a terminal state has been on screen at least once */
  seen: boolean;
  /** terminal + seen + the page was left: render the thin "上次…" bar */
  collapsed: boolean;

  /** bumped ONLY on a terminal transition, so the global toast can fire from
   *  whichever page is open (the page itself may be unmounted) */
  notice: { seq: number; phase: JobPhase; result: DoneData | null; error: string } | null;
}

let state: JobSessionState = {
  jobId: null,
  kind: null,
  title: '',
  phase: 'idle',
  stage: '',
  logs: [],
  detailLogs: [],
  result: null,
  error: '',
  path: '',
  isJson: false,
  seen: false,
  collapsed: false,
  notice: null,
};

const listeners = new Set<() => void>();

function set(partial: Partial<JobSessionState>): void {
  state = { ...state, ...partial };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

/** whole-state subscription (the page paints all of it). The nav indicator
 *  selects a primitive instead so a log line does not re-render the sidebar. */
export function useJobSession<T>(select: (s: JobSessionState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

/** non-React read, for handlers that must not close over a stale render */
export const jobSnapshot = (): JobSessionState => state;

/* ------------------------------------------------------------- attach */

let dispose: (() => void) | null = null;

/** The "new task" reset: every trace of the previous run, nothing of the file
 *  the user has just picked. */
function freshRunTrace(): Partial<JobSessionState> {
  return {
    stage: '', logs: [], detailLogs: [], result: null, error: '',
    seen: false, collapsed: false,
  };
}

/** Wire an SSE subscription to the store. Replaces any previous one.
 *
 *  ``replay`` is always on: the backend buffers a job's full event history and
 *  ``from_idx`` defaults to 0, so a late client repaints the run it never saw.
 *  Browser-side reconnects resume by themselves (the server now emits ``id:``
 *  and honours ``Last-Event-ID``), so we only ever attach once per job. */
function attach(jobId: string): void {
  dispose?.();
  const seq = (state.notice?.seq ?? 0) + 1;
  dispose = subscribeJob(jobId, {
    onStage: (s) => set({ stage: s }),
    onLog: (line, detail) =>
      set(detail
        ? { detailLogs: [...state.detailLogs.slice(-(DETAIL_CAP - 1)), line] }
        : { logs: [...state.logs.slice(-(LOG_CAP - 1)), line] }),
    onDone: (d) => {
      dispose = null;                    // subscribeJob closed the stream
      set({
        phase: 'done', result: d, error: '',
        notice: { seq, phase: 'done', result: d, error: '' },
      });
    },
    onError: (m) => {
      dispose = null;
      set({
        phase: 'error', error: m,
        notice: { seq, phase: 'error', result: null, error: m },
      });
    },
    onCancelled: () => {
      dispose = null;
      set({
        phase: 'cancelled', error: '',
        notice: { seq, phase: 'cancelled', result: null, error: '' },
      });
    },
  });
}

/* ------------------------------------------------------------- actions */

/** Create a run and start following it. Throws whatever createJob throws
 *  (an HTTP error string) -- the caller decides how to tell the user. */
export async function jobStart(body: StartBody): Promise<void> {
  const job = await createJob(body);
  set({
    ...freshRunTrace(),
    jobId: job.id,
    kind: job.kind,
    title: job.title,
    phase: 'running',
  });
  attach(job.id);
}

/** Record a failure that never became a job (preflight refusal, a refused
 *  createJob, a dead backend). Kept in the store rather than in the page so the
 *  message survives a page switch like every other outcome. */
export function jobFail(message: string): void {
  dispose?.();
  dispose = null;
  const seq = (state.notice?.seq ?? 0) + 1;
  set({
    phase: 'error', error: message,
    notice: { seq, phase: 'error', result: null, error: message },
  });
}

export function jobCancel(): void {
  if (state.jobId) cancelJob(state.jobId).catch(() => {});
}

/** The user picked a file.
 *
 *  Rule 1: a pick is the start of a NEW task, so a finished run's trace goes
 *  away immediately. It is NOT applied while a job is running -- the live log
 *  belongs to the file it is transcribing, and wiping it because the user
 *  dropped the next file would be a lie. */
export function jobPickFile(path: string, isJson: boolean): void {
  if (state.phase === 'running') {
    set({ path, isJson });
    return;
  }
  dispose?.();
  dispose = null;
  set({
    ...freshRunTrace(),
    jobId: null, kind: null, title: '',
    phase: 'idle',
    path, isJson,
  });
}

/** Rule 2, first half: the page has painted a terminal state, so the user has
 *  had their look at it. */
export function jobMarkSeen(): void {
  if (!state.seen && isTerminal(state.phase)) set({ seen: true });
}

/** Rule 2, second half: called from the page's unmount. Folding happens HERE
 *  (not on mount) so the screen the user is looking at never reflows under
 *  them; they come back to the clean page with the bar already closed. */
export function jobLeave(): void {
  if (isTerminal(state.phase) && state.seen && !state.collapsed) {
    set({ collapsed: true });
  }
}

/** the "查看" half of the bar: same screen as before, no data was ever lost */
export function jobExpand(): void {
  set({ collapsed: false });
}

/** Rule 3: the × -- forget the run entirely, file included. */
export function jobDismiss(): void {
  dispose?.();
  dispose = null;
  set({
    ...freshRunTrace(),
    jobId: null, kind: null, title: '',
    phase: 'idle',
    path: '', isJson: false,
  });
}

/* -------------------------------------------------------------- boot */

let bootPromise: Promise<void> | null = null;

/** Re-attach to a run this UI did not start (webview reload, renderer crash).
 *
 *  Only QUEUED/RUNNING jobs qualify: a settled one is either already on screen
 *  or it is ancient history, and resurrecting it would be a surprise. The
 *  backend's job map is the only record of a run that outlived the document, so
 *  this is the single question worth asking at startup.
 *
 *  Deduped per attempt (StrictMode double-mounts, the shell and the page both
 *  call it) but re-armable, because a cold start can reach this before uvicorn
 *  has bound its port. */
export function jobBoot(): Promise<void> {
  if (state.jobId) return Promise.resolve();
  if (bootPromise) return bootPromise;
  bootPromise = withBackendRetry(() => listJobs(), 6, 700)
    .then((jobs) => {
      // a job started while we were asking wins: never clobber a live one
      if (state.jobId) return;
      const live = jobs.find((j) => j.status === 'queued' || j.status === 'running');
      if (!live) return;
      set({
        ...freshRunTrace(),
        jobId: live.id,
        kind: live.kind,
        title: live.title,
        phase: 'running',
      });
      attach(live.id);
    })
    .catch(() => { /* backend not up: nothing to restore, the page says so */ })
    .finally(() => { bootPromise = null; });
  return bootPromise;
}
