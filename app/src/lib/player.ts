/**
 * AudioPlayer — refine-page playback kernel.
 *
 * Architecture lifted from trans-jimaku-web's lib/audioPlayer.ts (minus the
 * channel splitter and waveform that project needed), keeping its three
 * hard-won rules:
 *
 *  1. The state object is the SINGLE authority on play intent; DOM play/
 *     pause events are advisory. A late-firing `play` event arriving after
 *     the user already paused must never resurrect playback (their
 *     "偶发无法停止" bug, 20260815-2338).
 *  2. Progress is driven by requestAnimationFrame while playing and by the
 *     `timeupdate` event while paused — never both.
 *  3. One-shot segment playback ("listen to this sentence") parks the
 *     playhead AT the segment end instead of clearing to zero, so the same
 *     line can be replayed with one more click.
 *
 * Render budget (2026-11-15). The store is one object, so every `set` wakes
 * every subscriber — and the refine page holds a 400+-row subtitle list.
 * Pushing the clock at 60 Hz made playback feel slightly janky on a 55-min
 * file purely from re-rendering that list, so the clock is published at
 * ~10 Hz and callers select SLICES (usePlayerSelector) instead of the whole
 * state; React then bails out of any render whose slice did not change.
 * 100 ms is invisible: the readout is "m:ss" and the slider step is 50 ms.
 *
 * The audio element is a module-level singleton: leaving and re-entering
 * the refine page must not re-download / re-decode a large file.
 */
import { useSyncExternalStore } from 'react';
import { audioUrl } from './api';

export interface PlayerState {
  /** resolved audio path currently loaded ('' = nothing loaded) */
  path: string;
  playing: boolean;
  currentMs: number;
  /** 0 until loadedmetadata arrives */
  durationMs: number;
  rate: number;
  /** active one-shot segment (listen-to-sentence); null = full playback */
  segment: { startMs: number; endMs: number } | null;
  /** auto-scroll the entry list to the playing row */
  follow: boolean;
  /** last load/play failure (rendered on the card) */
  error: string;
}

const RATE_KEY = 'mai2srt.audioRate';
const FOLLOW_KEY = 'mai2srt.audioFollow';
export const RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];

/** m:ss for transport readouts (lives here, not in AudioCard: a component
 *  file exporting a helper breaks Vite Fast Refresh for the whole module) */
export function fmtClock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  return `${m}:${String(total - m * 60).padStart(2, '0')}`;
}

function loadRate(): number {
  const v = Number(localStorage.getItem(RATE_KEY));
  return RATES.includes(v) ? v : 1;
}

function loadFollow(): boolean {
  return localStorage.getItem(FOLLOW_KEY) !== '0'; // default on
}

let state: PlayerState = {
  path: '',
  playing: false,
  currentMs: 0,
  durationMs: 0,
  rate: loadRate(),
  segment: null,
  follow: loadFollow(),
  error: '',
};

const listeners = new Set<() => void>();

function set(partial: Partial<PlayerState>): void {
  state = { ...state, ...partial };
  listeners.forEach((l) => l());
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function usePlayer(): PlayerState {
  return useSyncExternalStore(subscribe, () => state);
}

/** Subscribe to ONE slice of the player state (primitive return values only:
 *  React compares the snapshot by identity, so a fresh object would defeat
 *  the bail-out it exists for). The refine page's 400-row list reads
 *  `playing` / `follow` / a derived row index this way, leaving the 10 Hz
 *  clock to the transport card alone. */
export function usePlayerSelector<T>(select: (s: PlayerState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

/* ----------------------------------------------------------- the element */

let el: HTMLAudioElement | null = null;
/** the media URL the element holds — survives route unmounts (reload skip) */
let loadedUrl = '';
let raf = 0;
/** last clock bucket pushed into the store (see CLOCK_STEP_MS) */
let lastBucket = -1;

/** store update granularity for the playing clock, in ms. The element ticks
 *  at the display rate (60 Hz); publishing that would re-render the subtitle
 *  list 60x a second for a number that only ever reads "m:ss". */
const CLOCK_STEP_MS = 100;

function cancelTick(): void {
  cancelAnimationFrame(raf);
  raf = 0;
  lastBucket = -1;      // the next tick publishes immediately
}

function tick(): void {
  if (!el) return;
  const ms = el.currentTime * 1000;
  const seg = state.segment;
  if (seg && ms >= seg.endMs) {
    // one-shot segment done: park at the end for an instant replay
    el.pause();
    el.currentTime = seg.endMs / 1000;
    set({ playing: false, segment: null, currentMs: seg.endMs });
    return;
  }
  const bucket = Math.floor(ms / CLOCK_STEP_MS);
  if (bucket !== lastBucket) {
    lastBucket = bucket;
    set({ currentMs: ms });
  }
  raf = requestAnimationFrame(tick);
}

function ensure(): HTMLAudioElement {
  if (el) return el;
  el = new Audio();
  el.preload = 'auto';
  el.playbackRate = state.rate;

  el.addEventListener('timeupdate', () => {
    if (!el || !el.paused) return; // rAF owns the clock while playing
    set({ currentMs: el.currentTime * 1000 });
  });
  const syncDuration = () => {
    if (el && Number.isFinite(el.duration)) set({ durationMs: el.duration * 1000 });
  };
  el.addEventListener('durationchange', syncDuration);
  el.addEventListener('loadedmetadata', syncDuration);
  el.addEventListener('ended', () => {
    cancelTick();
    if (el) el.currentTime = 0;
    set({ playing: false, segment: null, currentMs: 0 });
  });
  el.addEventListener('error', () => {
    cancelTick();
    const code = el?.error?.code;
    set({
      playing: false,
      segment: null,
      error: code ? `media error ${code}` : 'media error',
    });
  });
  el.addEventListener('play', () => {
    // advisory only: a late play event after an intentional pause is noise
    if (!state.playing || !el || el.paused) return;
    cancelTick();
    raf = requestAnimationFrame(tick);
  });
  el.addEventListener('pause', () => {
    cancelTick();
    if (state.playing) set({ playing: false }); // element-side pause syncs
  });
  return el;
}

/* -------------------------------------------------------------- actions */

/** Load a new resolved path; identical path is a no-op (remount-safe). */
export function playerLoad(path: string): void {
  const e = ensure();
  const url = audioUrl(path);
  if (url === loadedUrl) return;
  cancelTick();
  e.pause();
  loadedUrl = url;
  e.src = url;
  e.load();
  set({ path, playing: false, currentMs: 0, durationMs: 0, segment: null, error: '' });
}

export function playerUnload(): void {
  if (!el) return;
  cancelTick();
  el.pause();
  el.removeAttribute('src');
  el.load();
  loadedUrl = '';
  set({ path: '', playing: false, currentMs: 0, durationMs: 0, segment: null, error: '' });
}

export function playerPlay(): void {
  const e = ensure();
  if (!loadedUrl) return;
  set({ playing: true, error: '' }); // intent first: the store is authority
  e.play().catch((err) =>
    set({ playing: false, error: String(err).slice(0, 120) }));
}

export function playerPause(): void {
  set({ playing: false }); // intent first; the pause event then no-ops
  el?.pause();
}

export function playerToggle(): void {
  if (state.playing) playerPause();
  else playerPlay();
}

/** Manual seek (slider drag): always drops any one-shot segment. */
export function playerSeek(ms: number): void {
  const e = ensure();
  if (!loadedUrl) return;
  const max = state.durationMs || Number.MAX_SAFE_INTEGER;
  const v = Math.max(0, Math.min(ms, max));
  e.currentTime = v / 1000;
  set({ currentMs: v, segment: null });
}

/** Listen to one entry: seek to its start, play once, park at its end. */
export function playerPlaySegment(startMs: number, endMs: number): void {
  const e = ensure();
  if (!loadedUrl) return;
  const end = state.durationMs > 0 ? Math.min(endMs, state.durationMs) : endMs;
  const start = Math.max(0, Math.min(startMs, end));
  e.currentTime = start / 1000;
  set({ segment: { startMs: start, endMs: end }, currentMs: start, playing: true, error: '' });
  e.play().catch((err) =>
    set({ playing: false, segment: null, error: String(err).slice(0, 120) }));
}

export function playerSetRate(rate: number): void {
  if (el) el.playbackRate = rate;
  localStorage.setItem(RATE_KEY, String(rate));
  set({ rate });
}

export function playerSetFollow(follow: boolean): void {
  localStorage.setItem(FOLLOW_KEY, follow ? '1' : '0');
  set({ follow });
}
