// Shared subtitle-parameter specs + tiny helpers used by both the transcribe
// page (summary) and the refine workspace (sliders, editing).
import type { Key } from './i18n';
import type { PreviewWord } from './api';

export interface ParamSpec {
  key: string;
  label: Key;
  min: number;
  max: number;
  step: number;
  def: number;
  unit?: string;
}

/** the 8 user-owned knobs — bounds mirror SUBTITLE_PARAM_SPECS in config.py */
export const PARAM_SPECS: ParamSpec[] = [
  { key: 'max_duration', label: 'r.maxDuration', min: 4, max: 30, step: 0.5, def: 12, unit: 's' },
  { key: 'max_chars', label: 'r.maxChars', min: 20, max: 120, step: 1, def: 60 },
  { key: 'min_duration', label: 'r.minDuration', min: 0, max: 4, step: 0.1, def: 1.2, unit: 's' },
  { key: 'min_chars', label: 'r.minChars', min: 0, max: 20, step: 1, def: 5 },
  { key: 'split_pause', label: 'r.splitPause', min: 0.2, max: 2, step: 0.05, def: 0.8, unit: 's' },
  { key: 'merge_gap', label: 'r.mergeGap', min: 0.2, max: 3, step: 0.05, def: 0.8, unit: 's' },
  { key: 'tolerance', label: 'r.tolerance', min: 0, max: 1, step: 0.02, def: 0.2, unit: 's' },
  { key: 'expand', label: 'r.expand', min: 0, max: 0.6, step: 0.05, def: 0.25, unit: 's' },
];

export const PARAM_DEFAULTS: Record<string, number> = Object.fromEntries(
  PARAM_SPECS.map((s) => [s.key, s.def]),
);

/* ---------------------------------------------------- shared LLM toggle
   The same switch governs transcribe jobs and refine previews; persist it
   so both pages (and relaunches) agree. */

const LLM_KEY = 'mai2srt.useLlm';

export function loadUseLlm(): boolean {
  return localStorage.getItem(LLM_KEY) !== '0'; // default on
}

export function storeUseLlm(v: boolean): void {
  localStorage.setItem(LLM_KEY, v ? '1' : '0');
}

/* ------------------------------------------------------- text/time helpers
   Mirror of the backend CJK-aware glue (segment.rules._glue / join_words):
   CJK neighbours join without a space, everything else with one. */

// mirrors segment.rules.is_cjk: CJK unified, hiragana, katakana, hangul
const CJK_CHAR = /[一-鿿぀-ゟ゠-ヿ가-힯]/;

/** mirrors the backend _glue: no space when BOTH sides contain CJK */
export function glueText(a: string, b: string): string {
  if (!a) return b;
  if (!b) return a;
  return CJK_CHAR.test(a) && CJK_CHAR.test(b) ? a + b : `${a} ${b}`;
}

export function joinWords(words: PreviewWord[]): string {
  return words.reduce((acc, w) => glueText(acc, w.text), '');
}

/** dialogue-formatted text: 2+ non-empty lines, every one starting "- "
 *  (typing a text into this shape IS declaring a dialogue) */
export function isDialogueText(text: string): boolean {
  const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
  return lines.length >= 2 && lines.every((l) => l.startsWith('- '));
}

/** rebuild entry text from a word range: contiguous same-speaker words
 *  form runs; a range with 2+ runs renders as "- " lines (mirroring how
 *  the pipeline formats dialogue), a single-run range as plain glue */
export function wordsToText(ws: PreviewWord[]): { text: string; dialogue: boolean } {
  const runs: PreviewWord[][] = [];
  for (const w of ws) {
    const last = runs[runs.length - 1];
    if (last && last[0].speaker === w.speaker) last.push(w);
    else runs.push([w]);
  }
  if (runs.length <= 1) return { text: joinWords(ws), dialogue: false };
  return {
    text: runs.map((r) => `- ${joinWords(r)}`).join('\n'),
    dialogue: true,
  };
}

/** m:ss.cc display for entry word-boundary times in the refine list */
export function fmtEntryTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return `${m}:${s.toFixed(2).padStart(5, '0')}`;
}
