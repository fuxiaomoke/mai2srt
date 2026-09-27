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
   TWO different glue rules live in the backend, and conflating them inserted
   a space before CJK punctuation:

   * postprocess/dialogue.py::_glue joins two finished LINES -- no separator
     when both sides contain CJK (glueText below);
   * segment/rules.py::join_words rebuilds TEXT FROM WORDS -- a space only
     between two latin/alnum characters, so 。 or 、 (which are outside the CJK
     ranges above) glue directly (joinWords below). */

// mirrors dialogue.py::_is_cjk: CJK unified, hiragana, katakana, hangul
const CJK_CHAR = /[一-鿿぀-ゟ゠-ヿ가-힯]/;

// mirrors segment/rules.py::_LATIN_RE
const LATIN_CHAR = /[A-Za-z0-9]/;

/** mirrors dialogue.py's _glue: no space when BOTH TEXTS contain CJK. The
 *  rule for joining two finished lines (entry merge, dialogue stitching). */
export function glueText(a: string, b: string): string {
  if (!a) return b;
  if (!b) return a;
  return CJK_CHAR.test(a) && CJK_CHAR.test(b) ? a + b : `${a} ${b}`;
}

/** word-level glue: a space goes ONLY between two latin/alnum characters
 *  (mirrors rules.py::_needs_space, which tests the last char of the left and
 *  the first char of the right -- not "contains CJK"). */
export function glueWord(a: string, b: string): string {
  if (!a) return b;
  if (!b) return a;
  return LATIN_CHAR.test(a[a.length - 1]) && LATIN_CHAR.test(b[0])
    ? `${a} ${b}`
    : a + b;
}

/** mirrors rules.py::join_words: rebuild display text from word tokens. */
export function joinWords(words: PreviewWord[]): string {
  return words.reduce((acc, w) => glueWord(acc, w.text), '');
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

/* --------------------------------------------------- refine split helpers
   The refine split bar works at two granularities. Clause level ("句") groups
   the entry's words at the punctuation the pipeline itself offers as cut
   candidates, so the scissors appear only where a cut means something and the
   user never has to hunt for a comma; character level exposes every gap and
   marks the punctuation, because finding that comma among forty identical
   chips IS the work there.

   All marks are treated EQUALLY on purpose. candidates.py RANKS them (final >
   semicolon > ellipsis > comma > pause) because the automatic splitter must
   choose when a length limit forces a cut; a human picking a gap ranks
   nothing, and the glyph already says which mark it is. The set mirrors that
   module exactly -- deliberately no colon, which it does not treat as a
   boundary either. */

/** is this word (or the tail of it) punctuation the splitter can cut at?
 *  One set, not a ranking -- see the note above. */
const ANY_PUNCT = /[。？?！!.,，、;；…‥]$/;

export function isPunctuation(text: string): boolean {
  const t = (text ?? '').trim();
  return t.length > 0 && ANY_PUNCT.test(t);
}

/** Clause-level spans (INCLUSIVE absolute word indices) over words[w0..w1].

 *  A span ends after a PUNCTUATION word, and also where the speaker changes:
 *  a dialogue entry is drawn one line per speaker, so a cut there is what the
 *  user sees rather than an artefact of the word stream. A range with neither
 *  stays a single span.
 */
export function sentenceSpans(
  words: PreviewWord[],
  w0: number,
  w1: number,
): { s: number; t: number }[] {
  const out: { s: number; t: number }[] = [];
  let s = w0;
  for (let i = w0; i <= w1; i++) {
    const switches = i < w1 && words[i]?.speaker !== words[i + 1]?.speaker;
    if (isPunctuation(words[i]?.text ?? '') || switches || i === w1) {
      out.push({ s, t: i });
      s = i + 1;
    }
  }
  return out;
}
