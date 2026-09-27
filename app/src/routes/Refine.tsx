import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Bookmark, Check, FileJson, History, Loader2, Merge, Pencil,
  Play, Redo2, RotateCcw, Save, Scissors, Square, Trash2, Undo2, Wand2, X,
} from 'lucide-react';
import { open } from '@tauri-apps/plugin-dialog';
import { AudioCard } from '../components/AudioCard';
import { ArmDrain, ARM_MS, Button, Card, useToast, useTwoStepHint } from '../components/ui';
import { useI18n } from '../lib/i18n';
import {
  getStorage, getSubtitleParams, prepareAudio, previewEntries, renderSrt,
  revealPath, saveEditRecord, setSubtitleParams, warmStorage,
  withBackendRetry, type AudioInfo, type PreviewEntry, type PreviewWord,
  type SubtitleParams,
} from '../lib/api';
import {
  playerLoad, playerPause, playerPlaySegment, playerSeek, playerUnload,
  usePlayer,
} from '../lib/player';
import {
  PARAM_SPECS, fmtEntryTime, glueText, isDialogueText, isPunctuation,
  joinWords, sentenceSpans, wordsToText,
} from '../lib/subtitle';

/** a preview entry plus the "manually modified" marker */
type Editable = PreviewEntry & { edited?: true };

/** what produced the CURRENT entries: null = params-driven preview,
 *  'llm'/'manual' = restored or saved record structure */
type Provenance = 'llm' | 'manual' | null;

/** format a raw-line list: 2+ lines render as dash dialogue, one as plain */
function fmtLines(ls: string[]): { text: string; dialogue: boolean } {
  return ls.length >= 2
    ? { text: ls.map((x) => `- ${x}`).join('\n'), dialogue: true }
    : { text: ls[0] ?? '', dialogue: false };
}

/** entry text -> raw line contents (dash prefixes and empties stripped) */
function rawLines(text: string): string[] {
  return text.split('\n').map((l) => l.trim().replace(/^- /, '')).filter(Boolean);
}

/** local wall clock "YYYY-MM-DD HH:mm:ss" for the restore badge: the
 *  naive new Date().toISOString() slice printed UTC (8h off on Beijing
 *  time) while the badge shows the string verbatim; the backend's
 *  saved_at is local naive too, so both sources now agree */
function fmtLocalTime(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** undo depth cap: frames hold full entry copies, so 100 steps on a
 *  ~1500-row file stays in the low single-digit MBs */
const HISTORY_MAX = 100;

/** one COMMITTED screen state for the undo/redo stacks: every committed
 *  entries mutation (edit/split/merge/delete/regen) pushes the screen as
 *  it was BEFORE the change. Counters ride along so the "unsaved work"
 *  story stays honest after time travel; clock is the state's ordering
 *  number (see the clock refs below); params/lastGen are deliberately
 *  NOT captured -- undo restores content, never moves the sliders. */
interface Frame {
  entries: Editable[];
  editOps: number;
  provenance: Provenance;
  clock: number;
}

export function RefinePage() {
  const { t, tf } = useI18n();
  const [path, setPath] = useState('');
  const [autoLoaded, setAutoLoaded] = useState(false);
  const [params, setParams] = useState<SubtitleParams | null>(null);
  // the refine LLM toggle is a ONE-SHOT action: it starts off, running an
  // LLM split turns it back off automatically, so an accidental slider
  // touch can never silently re-trigger an expensive LLM round
  const [useLlm, setUseLlm] = useState(false);
  const [words, setWords] = useState<PreviewWord[]>([]);
  const [entries, setEntries] = useState<Editable[]>([]);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savingEdit, setSavingEdit] = useState(false);
  const [savedPath, setSavedPath] = useState('');
  const [restoredAt, setRestoredAt] = useState<string | null>(null);
  // a record was restored at load time (informational badge, NOT a dirty
  // marker: saved work cannot be lost, so it must not arm any gate)
  const [restoredLlm, setRestoredLlm] = useState(false);
  const [provenance, setProvenance] = useState<Provenance>(null);
  // restored record was made with different params than the current ones
  const [paramsDiffer, setParamsDiffer] = useState(false);
  // manual edit operations since the last anchor (load / save / restore):
  // the honest "unsaved work" counter -- deletes count too (no entry to
  // flag), so this supersedes entries.some(e => e.edited)
  const [editOps, setEditOps] = useState(0);
  const [banner, setBanner] = useState(false);
  // undo/redo: past frames vs states left behind by undo. Any new branch
  // clears redo; only a FILE SWITCH clears the stacks -- saving is not a
  // screen mutation, so the session history survives it.
  const [undoStack, setUndoStack] = useState<Frame[]>([]);
  const [redoStack, setRedoStack] = useState<Frame[]>([]);
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState('');
  const [splitIdx, setSplitIdx] = useState<number | null>(null);
  // split granularity: sentence level first (the common case); character
  // level exists for cutting inside a sentence, and highlights punctuation
  const [splitLevel, setSplitLevel] = useState<'sentence' | 'char'>('sentence');
  // two-click delete: first click arms (red icon), second click removes
  const [delIdx, setDelIdx] = useState<number | null>(null);
  // playback audio binding for the current file (from /api/preview)
  const [audioInfo, setAudioInfo] = useState<AudioInfo | null>(null);
  const player = usePlayer();
  const toast = useToast();
  // explains the two-click model on the first arm of the launch, whichever
  // delete in the app gets there first (shared with ConfirmIconButton)
  const teachTwoStep = useTwoStepHint();

  const timer = useRef<number | null>(null);
  const putTimer = useRef<number | null>(null);
  const seq = useRef(0);
  // params/path of the last successful preview -- ONLY a sync-guard for
  // the debounce effect (skip regen when nothing changed); regeneration
  // itself is no longer gated, the undo stack makes it reversible
  const lastGen = useRef<{ path: string; params: SubtitleParams } | null>(null);
  // the NEXT preview is a fresh load (mount auto-load / file switch): the
  // only moment an edit record may take over the display. Records are a
  // starting point, never a permanent overlay over live recomputes.
  const restoreNext = useRef(false);
  // screen-vs-disk ORDERING clock: every screen state the world has never
  // seen gets a higher number (global monotonic); undo/redo restore the
  // frame's recorded clock, so time travel moves the clock BACKWARD.
  // savedClock = the clock of the state last written to the EDIT RECORD
  // (saveEdit success / load-time restore). The file-switch auto-archive
  // compares them: after undoing PAST a save, the screen is OLDER than
  // the record on disk, and archiving it would clobber newer work with
  // older -- a hazard the pre-undo world could not have, because the
  // screen could only ever be the newest state.
  const clockMax = useRef(0);
  const screenClock = useRef(0);
  const savedClock = useRef<number | null>(null);
  // latest-value mirror for async callbacks: runPreview must snapshot the
  // CURRENT screen state, not the state of when its useCallback was built
  const live = useRef({ path, params, entries, editOps, provenance });
  live.current = { path, params, entries, editOps, provenance };

  const manualDirty = editOps > 0 || entries.some((e) => e.edited);
  // LLM in flight = paid work: a slider/reset/file change would trigger a
  // SECOND LLM call and discard the first, so lock the controls until it
  // settles. Deterministic previews are fast -- latest-wins is fine there.
  const llmBusy = busy && useLlm;
  // playing locks the same edit/param controls as llmBusy, for a structural
  // reason: the playhead maps onto entries and word indices, so nothing
  // that moves them may run while audio is live
  const locked = llmBusy || player.playing;
  // one focused inline operation at a time: while a text editor is open
  // (a DRAFT may be pending) the structural row tools are disabled --
  // implicitly closing the editor would strand the draft. Scissors and
  // the delete arming carry no data, so they just close each other.
  const editOpen = editingIdx !== null;
  // ONE inline tool at a time per screen: the text editor, the split panel or
  // an armed delete. Its row index doubles as the "a tool is open" flag --
  // while it is set, the actions that would collide are LOCKED rather than
  // silently swallowing the tool state (merging from any row shifts the word
  // indices an open draft or split panel is anchored to; playing a line while
  // its draft is open put two live states on one row).
  const toolRow = editingIdx ?? delIdx ?? splitIdx;
  const toolOpen = toolRow !== null;
  // undo/redo carry the same locks as the row tools: busy (an in-flight
  // regen would land on the restored screen and overwrite it), playing
  // (restoring moves the word indices the playhead maps onto), editOpen
  // (the open draft's textarea owns Ctrl+Z), and the save awaits (the
  // POST must not race a screen swap)
  const histLock = busy || player.playing || editOpen || saving || savingEdit;
  const canUndo = !histLock && undoStack.length > 0;
  const canRedo = !histLock && redoStack.length > 0;
  // Ctrl+S mirrors the saveEdit button's enable condition, except it
  // also yields while a draft editor is open: the open draft owns the
  // keyboard (same rule as Ctrl+Z -> native undo), and archiving under
  // an uncommitted draft would write a state the user is not looking at
  const canSaveEdit = !!path && !!params && entries.length > 0 &&
    !busy && !saving && !savingEdit && !editOpen;

  /* ------------------------------------------------------------ data load */

  // mount: persisted params + auto-load the latest transcription.
  // withBackendRetry: the dev chain can serve this page before uvicorn has
  // bound its port, and a one-shot fetch would strand the page empty behind
  // a "backend offline" toast
  useEffect(() => {
    withBackendRetry(() => getSubtitleParams())
      .then(({ params: p, last_mai_json }) => {
        setParams(p);
        if (last_mai_json) {
          restoreNext.current = true;
          setPath(last_mai_json);
          setAutoLoaded(true);
        }
      })
      .catch(() => toast('error', t('r.toast.offline')));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const runPreview = useCallback(
    async (p: string, pr: SubtitleParams, llm: boolean) => {
      if (!p) return;
      const mySeq = ++seq.current;
      const restore = restoreNext.current;
      restoreNext.current = false;
      // this regen REPLACES the screen: park the current state on the
      // undo stack -- every committed change is reversible now, the
      // stack is the one recovery mechanism (no separate restore point).
      // The banner nudge is armed only when the replaced screen held
      // irreplaceable work (manual edits / a restored structure).
      const lv = live.current;
      let replacedValued = false;
      if (lv.path === p) {
        if (lv.entries.length > 0) {
          pushHistory();
          replacedValued = lv.editOps > 0 || lv.provenance !== null;
        }
      } else {
        // switching files: history from another file is meaningless; the
        // incoming screen is a brand-new state and the record status of
        // the new file is unknown until a restore happens
        setUndoStack([]);
        setRedoStack([]);
        screenClock.current = ++clockMax.current;
        savedClock.current = null;
      }
      setBusy(true);
      try {
        const d = await previewEntries(p, pr, llm);
        if (mySeq !== seq.current) return; // a newer request won
        setWords(d.words);
        setAudioInfo(d.audio ?? null);
        if (llm) {
          // one-shot semantics: flip back off right away, and record the
          // entries as "generated with llm off" so the sync guard does not
          // regenerate over the LLM result we just received
          setUseLlm(false);
          if (d.llm_note === 'unavailable') {
            toast('warn', t('r.toast.llmUnavailable'));
          } else if (d.llm_note) {
            toast('warn', tf('r.toast.llmFailed', { err: String(d.llm_note).slice(0, 120) }));
          }
        }
        lastGen.current = { path: p, params: { ...pr } };
        setSplitIdx(null);
        setEditingIdx(null);
        setDelIdx(null);
        setEditOps(0);
        // banner: armed at push time, applied on success -- a FAILED
        // regen never replaced anything, so it must not claim it did;
        // `was` keeps the nudge alive across a chain of regens (the
        // valued frame stays buried underneath, still one jump away)
        setBanner((was) => replacedValued || was);
        if (restore && d.edit) {
          // load-time restore: starting point only, NO edited marks
          // (saved work cannot be lost, so it must not look unsaved);
          // dialogue flags re-sync from the text shape so stale records
          // (saved before the shape-sync rule existed) self-heal on open
          setEntries(
            d.edit.entries.map((e) => ({ ...e, dialogue: isDialogueText(e.text) })),
          );
          const isLlm = d.edit.origin === 'llm';
          setProvenance(isLlm ? 'llm' : 'manual');
          setRestoredLlm(isLlm);
          setRestoredAt(isLlm ? null : (d.edit.saved_at || '').replace('T', ' '));
          const rp = d.edit.params;
          setParamsDiffer(
            !!rp && PARAM_SPECS.some((sp) => rp[sp.key] !== pr[sp.key]),
          );
          // the record just took over the screen: disk and screen are
          // the same state, so the auto-archive guard must see them as
          // equal (undoing back to the pre-restore screen will then be
          // OLDER than disk and must not clobber the record)
          savedClock.current = screenClock.current;
        } else {
          // fresh preview: same flag sync (no-op for pipeline output,
          // whose dialogue text is always dash-shaped)
          setEntries(d.entries.map((e) => ({ ...e, dialogue: isDialogueText(e.text) })));
          setProvenance(llm && d.llm_note == null ? 'llm' : null);
          setRestoredAt(null);
          setRestoredLlm(false);
          setParamsDiffer(false);
        }
      } catch (e) {
        if (mySeq !== seq.current) return;
        toast('error', String(e).slice(0, 160));
        if (lastGen.current?.path !== p) {
          // this was loading a DIFFERENT file: show nothing rather than the
          // previous file's entries sitting under the new file's name
          setEntries([]);
          setWords([]);
          setAudioInfo(null);
          setRestoredAt(null);
          setRestoredLlm(false);
          setProvenance(null);
          setParamsDiffer(false);
          setEditOps(0);
          setBanner(false);
          setUndoStack([]);
          setRedoStack([]);
          lastGen.current = null;
        }
        // otherwise a same-file re-preview failed: the screen still shows
        // the last good state for THIS file -- keep it (and any parked
        // restore point); the next param change retries
      } finally {
        if (mySeq === seq.current) setBusy(false);
      }
    },
    [toast, t, tf],
  );

  // debounced regenerate on path/param/llm change; not gated anymore: the
  // undo stack makes every regeneration reversible
  useEffect(() => {
    if (!path || !params) return;
    // already in sync: regenerating now would wipe the edits we just kept
    const g = lastGen.current;
    if (
      g && g.path === path && !useLlm && entries.length > 0 &&
      PARAM_SPECS.every((s) => g.params[s.key] === params[s.key])
    ) {
      return;
    }
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => runPreview(path, params, useLlm), 300);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
    // entries intentionally not a dep: edits must not trigger regeneration
  }, [path, params, useLlm, runPreview]); // eslint-disable-line react-hooks/exhaustive-deps

  // persist param changes (debounced) so transcribe jobs use the same set
  useEffect(() => {
    if (!params) return;
    if (putTimer.current) window.clearTimeout(putTimer.current);
    putTimer.current = window.setTimeout(
      () => setSubtitleParams(params).catch(() => {}),
      500,
    );
  }, [params]);

  async function pick() {
    // open straight in the storage library -- that is where projects live
    // now. Seeded from the warm cache (fresh after any dir change); a
    // non-existent folder (fresh install, nothing stored yet) falls back
    // to the dialog's own default instead of a dead path.
    let defaultPath: string | undefined;
    try {
      const s = warmStorage() ?? (await getStorage());
      if (s?.exists) defaultPath = s.dir;
    } catch { /* offline: dialog default */ }
    const p = await open({
      multiple: false,
      defaultPath,
      filters: [{ name: 'mai2srt json', extensions: ['json'] }],
    });
    if (typeof p !== 'string') return;
    // unsaved work on screen: archive it before switching -- a file switch
    // must never silently destroy refinement (record only, no forced srt).
    // The clock guard is the undo-era addition: after undoing PAST a
    // saveEdit, the screen shows an OLDER state than the record on disk
    // (editOps restored to >0 by the frame) -- archiving it would
    // overwrite the newer record. Only archive when the screen is
    // strictly NEWER than what was last written.
    const lv = live.current;
    if (
      lv.path && lv.entries.length > 0 &&
      (lv.editOps > 0 || lv.entries.some((e) => e.edited)) &&
      screenClock.current > (savedClock.current ?? -1)
    ) {
      try {
        await saveEditRecord(
          lv.path,
          lv.entries.map(({ w0, w1, text, dialogue }) => ({ w0, w1, text, dialogue })),
          lv.params ?? {},
        );
        toast('ok', t('r.toast.autoSaved'));
      } catch (e) {
        // do NOT switch: switching now would destroy the very work the
        // archive was supposed to protect
        toast('error', tf('r.toast.autoSaveFailed', { err: String(e).slice(0, 120) }));
        return;
      }
    }
    restoreNext.current = true;
    seq.current++; // invalidate any in-flight response for the OLD file
    playerPause(); // the old file's audio stops with its word timeline
    setUndoStack([]);
    setRedoStack([]);
    savedClock.current = null;
    setBanner(false);
    // the switch invalidates the CURRENT screen: entries/provenance/
    // editOps belong to the previous file. Clearing them NOW matters
    // because runPreview runs 300ms later (debounce), when the live
    // mirror already says path=NEW but still carried the OLD file's
    // valued state -- pushHistory then misread it as "a valued
    // SAME-file screen" and parked a CROSS-FILE undo frame: the
    // banner always popped after a switch, and undo applied the
    // previous project's entries (word indexes from another file ->
    // mixed rows, out-of-bounds errors on save)
    setEntries([]);
    setEditOps(0);
    setProvenance(null);
    setRestoredLlm(false);
    setRestoredAt('');
    setParamsDiffer(false);
    setPath(p);
    setAutoLoaded(false);
    setSavedPath('');
  }

  /* ------------------------------------------------------------- playback
     * The audio element lives in lib/player (module singleton); here we only
     * bind it to the resolved path and derive the playing row. The row is
     * DERIVED (binary search), never stored: edits that move boundaries can
     * never leave a stale "playing" marker behind (trans-jimaku-web rule). */

  const resolvedAudio = audioInfo?.resolved ?? '';
  // pending video/exotic source: extract the track now (the FIRST open
  // pays the remux/transcode, every later one hits the cache; successful
  // transcriptions prewarm it in the background). On failure fall back
  // to a missing-style display so the "extracting" hint never lies.
  useEffect(() => {
    if (!audioInfo?.pending || !audioInfo.pending_path || !path) return;
    let dead = false;
    prepareAudio(path)
      .then((audio) => { if (!dead) setAudioInfo(audio); })
      .catch((e) => {
        if (dead) return;
        toast('error', String(e).slice(0, 160));
        setAudioInfo((a) => (a ? { ...a, pending: false, missing: a.pending_path } : a));
      });
    return () => { dead = true; };
  }, [audioInfo, path]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (resolvedAudio) playerLoad(resolvedAudio);
    else playerUnload();
  }, [resolvedAudio]);
  // leaving the page pauses; the element keeps its loaded media so coming
  // back is instant (no re-download / re-decode)
  useEffect(() => () => playerPause(), []);

  /** the entry the playhead is inside (-1 = stopped / between entries) */
  const playingIdx = useMemo(() => {
    if (!player.playing) return -1;
    const t = player.currentMs / 1000;
    let lo = 0;
    let hi = entries.length - 1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const w0 = words[entries[mid].w0];
      const w1 = words[entries[mid].w1];
      if (!w0 || !w1) return -1;
      if (w1.end < t) lo = mid + 1;
      else if (w0.start > t) hi = mid - 1;
      else return mid;
    }
    return -1;
  }, [player.playing, player.currentMs, entries, words]);

  /** the row whose one-shot listen segment is currently playing (-1 none) */
  const segmentRow = useMemo(() => {
    if (!player.playing || !player.segment) return -1;
    const segStart = player.segment.startMs;
    return entries.findIndex((e) => {
      const w0 = words[e.w0];
      return !!w0 && Math.abs(w0.start * 1000 - segStart) < 300;
    });
  }, [player.playing, player.segment, entries, words]);

  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  // follow mode: the playing row is centered in the viewport, not nudged
  // in at the nearest edge -- with 'nearest' every new row entered at the
  // BOTTOM of the list, so the user ended up reading along the floor
  useEffect(() => {
    if (playingIdx < 0 || !player.follow) return;
    rowRefs.current[playingIdx]?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [playingIdx, player.follow]);

  /** listen-to-this-line: one-shot segment play; clicking the playing row's
   *  button again stops it (the button stays visible while its row plays) */
  function toggleListen(i: number) {
    if (i === segmentRow) {
      playerPause();
      return;
    }
    const e = entries[i];
    const w0 = words[e.w0];
    const w1 = words[e.w1];
    if (!w0 || !w1) return;
    playerPlaySegment(w0.start * 1000, w1.end * 1000);
  }

  /** click ANYWHERE in a row: with follow-playback on, fly the playhead
   *  to the row's first word. Playing continues seamlessly -- the rAF
   *  clock simply reads the new position next frame, so the card slider
   *  and the follow highlight re-land together, nothing pauses or
   *  restarts. Follow off = no seek: follow and row-jump are one feature
   *  (clicking rows must not move audio behind a "static view"). */
  function seekRow(i: number, ev: React.MouseEvent) {
    // inner controls own their clicks (listen/tools/split gaps/editor);
    // the split word-chip panel too -- clicking there picks a cut point,
    // not the row
    if ((ev.target as HTMLElement).closest('button, textarea, input, a, [data-noseek]')) return;
    if (!player.follow) return;
    const w0 = words[entries[i]?.w0];
    if (!w0) return;
    playerSeek(w0.start * 1000);
  }

  /* ------------------------------------------------------------- undo/redo
   * Snapshot-stack model: every COMMITTED entries mutation pushes the
   * pre-change screen; undo pops back, redo replays. Frames capture the
   * counters that describe the screen (editOps/provenance); params and
   * lastGen stay untouched -- undo never moves the sliders, and lastGen
   * already matches the current params, so the restored screen must not
   * look "out of sync" or the debounce would instantly regenerate over
   * it (the rule the old restoreSnapshot established). */

  /** deep-enough copy of the CURRENT screen as one frame */
  function screenFrame(): Frame {
    const lv = live.current;
    return {
      entries: lv.entries.map((e) => ({ ...e })),
      editOps: lv.editOps,
      provenance: lv.provenance,
      clock: screenClock.current,
    };
  }

  /** park the current screen as the newest undo frame -- call BEFORE the
   *  mutation applies; the incoming screen is a brand-new state, so the
   *  clock moves forward; any new branch clears redo */
  function pushHistory() {
    if (live.current.entries.length === 0) return;
    setUndoStack((s) => [...s, screenFrame()].slice(-HISTORY_MAX));
    setRedoStack([]);
    screenClock.current = ++clockMax.current;
  }

  /** put a frame back on the screen; tool state resets like every other
   *  structural transition, and the clock travels back with it (the
   *  restored state is OLD, not new) */
  function applyFrame(f: Frame) {
    screenClock.current = f.clock;
    setEntries(f.entries.map((e) => ({ ...e })));
    setEditOps(f.editOps);
    setProvenance(f.provenance);
    setBanner(false);
    setSplitIdx(null);
    setEditingIdx(null);
    setDelIdx(null);
  }

  function undo() {
    if (!canUndo) return;
    const cur = screenFrame();
    setRedoStack((s) => [...s, cur].slice(-HISTORY_MAX));
    setUndoStack(undoStack.slice(0, -1));
    applyFrame(undoStack[undoStack.length - 1]);
  }

  function redo() {
    if (!canRedo) return;
    const cur = screenFrame();
    setUndoStack((s) => [...s, cur].slice(-HISTORY_MAX));
    setRedoStack(redoStack.slice(0, -1));
    applyFrame(redoStack[redoStack.length - 1]);
  }

  /** index of the nearest undo frame that holds irreplaceable work */
  function valuedIdx(): number {
    for (let j = undoStack.length - 1; j >= 0; j--) {
      const f = undoStack[j];
      if (f.editOps > 0 || f.provenance !== null) return j;
    }
    return -1;
  }

  /** banner jump: straight back to the buried valued frame, stepping
   *  over any regen outputs stacked on top of it (each popped state
   *  enters redo in timeline order, so redo walks forward again) */
  function undoValued() {
    if (histLock) return;
    const k = valuedIdx();
    if (k < 0) { undo(); return; }
    const cur = screenFrame();
    setRedoStack((s) => [...s, ...undoStack.slice(k + 1), cur].slice(-HISTORY_MAX));
    setUndoStack(undoStack.slice(0, k));
    applyFrame(undoStack[k]);
  }

  /** the frame the banner jumps to (label source in the UI) */
  const valuedFrame = valuedIdx() >= 0 ? undoStack[valuedIdx()] : null;

  // Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y / Ctrl+S. While a text editor is open
  // the textarea's NATIVE undo owns the keystroke (histLock folds editOpen
  // in): the global handler stays silent and lets it bubble. The ref
  // pattern keeps the listener mounted once while always seeing the
  // latest stacks and locks.
  const keyHandler = useRef<(ev: KeyboardEvent) => void>(() => {});
  keyHandler.current = (ev) => {
    // Escape backs out of a pending row tool, the same gesture the text
    // editor already offers. It runs before the Ctrl/Meta gate below so a
    // bare Escape is enough.
    if (ev.key === 'Escape') {
      setDelIdx(null);
      setSplitIdx(null);
      return;
    }
    if (!(ev.ctrlKey || ev.metaKey)) return;
    const k = ev.key.toLowerCase();
    if (k === 'z' && !ev.shiftKey && canUndo) {
      ev.preventDefault();
      undo();
    } else if (((k === 'z' && ev.shiftKey) || k === 'y') && canRedo) {
      ev.preventDefault();
      redo();
    } else if (k === 's' && canSaveEdit) {
      // checkpoint the edit record: the same 保存编辑结果 action as the
      // sidebar button, under its exact enable condition
      ev.preventDefault();
      saveEdit();
    }
  };
  useEffect(() => {
    const h = (ev: KeyboardEvent) => keyHandler.current(ev);
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  // An armed delete must not sit there indefinitely: it clears itself after
  // exactly the window the row's draining line draws (ARM_MS, shared with
  // every other destructive control). Otherwise a row could stay armed through
  // unrelated work and fire on a much later, unrelated second click -- the
  // opposite of what a confirmation is for.
  useEffect(() => {
    if (delIdx === null) return;
    const id = window.setTimeout(() => setDelIdx(null), ARM_MS);
    return () => window.clearTimeout(id);
  }, [delIdx]);

  /* ---------------------------------------------------------- entry edits */

  function commitText(i: number) {
    const text = editDraft.trim();
    const cur = entries[i];
    if (text && cur && text !== cur.text) {
      // typing "- a。\n- b。" IS declaring a dialogue (and un-dashing the
      // text un-declares it): sync the flag so the count, styling and
      // render all agree with what the text says
      const dialogue = isDialogueText(text);
      pushHistory();
      setEntries((es) => es.map((e, j) => (j === i ? { ...e, text, dialogue, edited: true } : e)));
      setEditOps((n) => n + 1);
      setBanner(false);
    }
    setEditingIdx(null);
  }

  /** Exit whichever inline tool is open on a row OTHER than `keep`
   *  (null = any row): the outside-click path, and what every tool icon runs
   *  before it takes over. A pending text draft is COMMITTED, never dropped --
   *  the undo stack only stores committed screens, so a discarded draft would
   *  be unrecoverable. */
  function closeTool(keep: number | null) {
    if (editingIdx !== null && editingIdx !== keep) commitText(editingIdx);
    if (splitIdx !== null && splitIdx !== keep) setSplitIdx(null);
    if (delIdx !== null && delIdx !== keep) setDelIdx(null);
  }

  // Clicking anywhere OUTSIDE THE ROW THAT OWNS THE OPEN TOOL exits it --
  // another row counts, wherever in it you click (its text, its header, its
  // padding, one of its buttons: each of those either closes the tool itself
  // or has nothing to do with it). Only the owning row is exempt, so clicking
  // its own text/padding never interrupts what you are doing there.
  //
  // CAPTURE phase, and that is load-bearing: React flushes a discrete click
  // synchronously, so by the time a BUBBLE-phase document listener runs, the
  // row it just opened an editor in has re-rendered -- the clicked <p> has been
  // replaced and detached, closest('[data-row]') finds nothing, and the guard
  // fails. The listener then "closed" the editor the same click had opened.
  // (Synthetic el.click() updates state later, so the node is still attached
  // and the bug hides -- only real mouse input shows it.) Capture runs before
  // React touches the DOM, where the target is still where it was clicked.
  const closeToolRef = useRef(closeTool);
  const toolRowRef = useRef<number | null>(toolRow);
  const outsideClickRef = useRef<(row: number | null) => void>(() => {});
  useEffect(() => {
    closeToolRef.current = closeTool;
    toolRowRef.current = toolRow;
    outsideClickRef.current = (row) => {
      if (toolRowRef.current === row) return;
      closeToolRef.current(null);
    };
  });
  useEffect(() => {
    const onDocClick = (ev: MouseEvent) => {
      const el = (ev.target as HTMLElement | null)?.closest?.('[data-row]');
      const raw = el ? Number((el as HTMLElement).dataset.row) : NaN;
      outsideClickRef.current(Number.isNaN(raw) ? null : raw);
    };
    document.addEventListener('click', onDocClick, true);
    return () => document.removeEventListener('click', onDocClick);
  }, []);

  /** dialogue + plain: the plain text glues INTO one of the dialogue's
   *  lines -- never across the dash format. Target line = the one whose
   *  speaker matches the plain entry's (via alignLines) when exactly one
   *  line matches; otherwise the ADJACENT line (last when the plain comes
   *  after, first when before). pAfter = plain is the later entry. */
  function mergeDlgPlain(d: Editable, p: Editable, pAfter: boolean): Editable {
    const lines = rawLines(d.text);
    let target = pAfter ? lines.length - 1 : 0;
    const al = alignLines(d);
    const pSpk = new Set(words.slice(p.w0, p.w1 + 1).map((w) => w.speaker));
    if (al && pSpk.size === 1) {
      const sp = [...pSpk][0];
      const hits: number[] = [];
      al.ranges.forEach((r, idx) => {
        if (r.spk.size === 1 && [...r.spk][0] === sp) hits.push(idx);
      });
      if (hits.length === 1) target = hits[0];
    }
    lines[target] = pAfter
      ? glueText(lines[target], p.text)
      : glueText(p.text, lines[target]);
    const f = fmtLines(lines);
    return {
      w0: Math.min(d.w0, p.w0), w1: Math.max(d.w1, p.w1),
      text: f.text, dialogue: f.dialogue, edited: true,
    };
  }

  /** dialogue + dialogue: pair lines by speaker (a's line order wins,
   *  b-only speakers append); when either side cannot be aligned, keep
   *  every line intact rather than mashing two lines into one */
  function mergeDlgDlg(a: Editable, b: Editable): Editable {
    const la = rawLines(a.text);
    const lb = rawLines(b.text);
    const al = alignLines(a);
    const bl = alignLines(b);
    let out: string[];
    if (al && bl) {
      const used = new Set<number>();
      out = al.ranges.map((r, i) => {
        let merged = la[i];
        if (r.spk.size === 1) {
          const sp = [...r.spk][0];
          const j = bl.ranges.findIndex((rr, idx) =>
            !used.has(idx) && rr.spk.size === 1 && [...rr.spk][0] === sp);
          if (j >= 0) {
            used.add(j);
            merged = glueText(merged, lb[j]);
          }
        }
        return merged;
      });
      lb.forEach((l, idx) => { if (!used.has(idx)) out.push(l); });
    } else {
      out = [...la, ...lb];
    }
    const f = fmtLines(out);
    return {
      w0: a.w0, w1: b.w1,
      text: f.text, dialogue: f.dialogue, edited: true,
    };
  }

  /** structure-aware merge of adjacent entries a (earlier) + b (later) */
  function mergeEntries(a: Editable, b: Editable): Editable {
    const dlgA = a.dialogue || isDialogueText(a.text);
    const dlgB = b.dialogue || isDialogueText(b.text);
    if (dlgA && dlgB) return mergeDlgDlg(a, b);
    if (dlgA) return mergeDlgPlain(a, b, true);
    if (dlgB) return mergeDlgPlain(b, a, false);
    return {
      w0: a.w0, w1: b.w1,
      text: glueText(a.text, b.text), dialogue: false, edited: true,
    };
  }

  function mergeWithNext(i: number) {
    pushHistory();
    setEntries((es) => {
      if (i >= es.length - 1) return es;
      return [...es.slice(0, i), mergeEntries(es[i], es[i + 1]), ...es.slice(i + 2)];
    });
    setEditOps((n) => n + 1);
    setBanner(false);
    setDelIdx(null);
    setSplitIdx(null);
    setEditingIdx(null);
  }

  /** map each dash line of a dialogue entry back to its contiguous word
   *  range by deterministic whitespace-insensitive glue matching; each
   *  range carries its speaker set for merge attribution. null = the text
   *  no longer matches its words (custom retext). Shared by split+merge. */
  function alignLines(e: Editable) {
    const lines = rawLines(e.text);
    if (lines.length < 2) return null;
    const norm = (s: string) => s.replace(/\s+/g, '');
    const ranges: { s: number; t: number; spk: Set<string | null> }[] = [];
    let w = e.w0;
    for (const ln of lines) {
      let glued = '';
      const s = w;
      while (w <= e.w1 && norm(glued) !== norm(ln)) {
        glued = glueText(glued, words[w].text);
        w += 1;
      }
      if (norm(glued) !== norm(ln)) return null;
      ranges.push({
        s, t: w - 1,
        spk: new Set(words.slice(s, w).map((x) => x.speaker)),
      });
    }
    return w === e.w1 + 1 ? { lines, ranges } : null;
  }

  /** split entry i between its k-th and (k+1)-th word (k = index within entry) */
  function splitAt(i: number, k: number) {
    const e = entries[i];
    if (!e || k < 0 || e.w0 + k >= e.w1) return;
    const splitAbs = e.w0 + k; // last word index of the left half
    let left: Editable | null = null;
    let right: Editable | null = null;
    let aligned = false;
    // gate on the TEXT SHAPE first, the flag second: the declared
    // structure lives in the text, and records saved before flag-sync
    // existed can carry dialogue=false under dash-shaped text (stale)
    const al = (e.dialogue || isDialogueText(e.text)) ? alignLines(e) : null;
    if (al) {
      // preferred path: cut along the DECLARED line structure. This covers
      // pipeline dialogue AND hand-typed dialogue on single-speaker words
      // (where speaker labels carry no structure at all).
      const j = al.ranges.findIndex((r) => splitAbs >= r.s && splitAbs <= r.t);
      if (j >= 0) {
        aligned = true;
        // untouched lines keep their ORIGINAL text (user wording
        // survives); only the cut line is rebuilt from its words
        const L = fmtLines([
          ...al.lines.slice(0, j),
          joinWords(words.slice(al.ranges[j].s, splitAbs + 1)),
        ].filter(Boolean));
        const R = fmtLines([
          joinWords(words.slice(splitAbs + 1, al.ranges[j].t + 1)),
          ...al.lines.slice(j + 1),
        ].filter(Boolean));
        left = { w0: e.w0, w1: splitAbs, text: L.text, dialogue: L.dialogue, edited: true };
        right = { w0: splitAbs + 1, w1: e.w1, text: R.text, dialogue: R.dialogue, edited: true };
      }
    }
    if (!left || !right) {
      // fallback: rebuild halves from speaker runs (labels carry the
      // structure for pipeline dialogue; hand-typed lines that no longer
      // match their words land here too)
      const leftH = wordsToText(words.slice(e.w0, splitAbs + 1));
      const rightH = wordsToText(words.slice(splitAbs + 1, e.w1 + 1));
      left = { w0: e.w0, w1: splitAbs, text: leftH.text, dialogue: leftH.dialogue, edited: true };
      right = { w0: splitAbs + 1, w1: e.w1, text: rightH.text, dialogue: rightH.dialogue, edited: true };
    }
    setEntries((es) => [...es.slice(0, i), left!, right!, ...es.slice(i + 1)]);
    // the fallback rebuild loses a custom retext (aligned cuts do not:
    // untouched lines keep their original wording) -- say so then
    if (!aligned && e.edited) toast('warn', t('r.toast.splitRebuilt'));
    pushHistory();
    setEditOps((n) => n + 1);
    setBanner(false);
    setSplitIdx(null);
    setDelIdx(null);
  }


  function deleteEntry(i: number) {
    pushHistory();
    setEntries((es) => es.filter((_, j) => j !== i));
    setEditOps((n) => n + 1);
    setBanner(false);
    setDelIdx(null);
    setSplitIdx(null);
    setEditingIdx(null);
  }

  /* ------------------------------------------------------------------ save */

  async function save() {
    if (!path || !params || entries.length === 0) return;
    // NOTE: save() exports the .srt but does NOT write the edit record,
    // so savedClock stays untouched -- the auto-archive guard tracks the
    // RECORD's state, not the srt's
    setSaving(true);
    try {
      const r = await renderSrt(
        path,
        entries.map(({ w0, w1, text, dialogue }) => ({ w0, w1, text, dialogue })),
        params,
        // localized suffix (_精修 / _refined): the refined export must
        // coexist with the initial transcription's .srt, not clobber it
        t('r.refineSuffix'),
      );
      setSavedPath(r.srt_path);
      setRestoredAt(fmtLocalTime(new Date()));
      // saved = durable: clear the session-dirty markers, the structure is
      // now the manual record on disk
      setRestoredLlm(false);
      setProvenance('manual');
      setEditOps(0);
      setEntries((es) => es.map((e) => {
        const c = { ...e };
        delete c.edited;
        return c;
      }));
      setBanner(false);
      toast('ok', tf('r.toast.saved', { name: r.srt_path.split(/[\\/]/).pop() ?? '' }));
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    } finally {
      setSaving(false);
    }
  }

  /** checkpoint WITHOUT writing the .srt: the edit record becomes the
   *  durable baseline -- in-session recovery is the undo stack's job,
   *  this one survives restarts and file switches */
  async function saveEdit() {
    if (!path || !params || entries.length === 0) return;
    setSavingEdit(true);
    // clock of the PAYLOAD (captured at call start -- the screen cannot
    // change during the await, but capture anyway so the guard can never
    // mark a state as saved that wasn't the one written)
    const c0 = screenClock.current;
    try {
      await saveEditRecord(
        path,
        entries.map(({ w0, w1, text, dialogue }) => ({ w0, w1, text, dialogue })),
        params,
      );
      savedClock.current = c0;
      setRestoredAt(fmtLocalTime(new Date()));
      setRestoredLlm(false);
      setProvenance('manual');
      setEditOps(0);
      setEntries((es) => es.map((e) => {
        const c = { ...e };
        delete c.edited;
        return c;
      }));
      setBanner(false);
      toast('ok', t('r.toast.editSaved'));
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    } finally {
      setSavingEdit(false);
    }
  }

  /** drop the session screen and reload the archived edit record: the
   *  DURABLE checkpoint on disk, complementing the session undo stack --
   *  and the deep reset when the history no longer reaches far enough */
  function restoreArchive() {
    if (!path || !params || busy) return;
    restoreNext.current = true;
    runPreview(path, params, false);
  }

  const dialogueCount = useMemo(() => entries.filter((e) => e.dialogue).length, [entries]);

  /* ------------------------------------------------------------------ ui */

  return (
    <div className="mx-auto flex h-full w-full max-w-[1400px] gap-6 px-8 py-7">
      {/* left: file + params + actions; scrolls rather than crushing its
          children when the window is too short. [&>*]:shrink-0 keeps EVERY
          child at natural height (the saved-path link and any future card
          below the save button included) so they overflow into the
          scrollbar instead of being squashed out of existence. */}
      <div className="flex min-h-0 w-[290px] shrink-0 flex-col gap-4 overflow-y-auto [&>*]:shrink-0">
        <Card
          className={`flex cursor-pointer items-center gap-3 p-4 ${locked ? 'pointer-events-none opacity-60' : ''}`}
          hover
        >
          <div onClick={pick} className="flex min-w-0 flex-1 items-center gap-3">
            <span className="grid h-10 w-10 shrink-0 place-items-center rounded-(--radius-s) bg-primary-dim text-primary">
              <FileJson size={18} strokeWidth={1.8} />
            </span>
            <div className="min-w-0">
              <p className="truncate text-[13px] font-semibold text-ink-1">
                {path ? path.split(/[\\/]/).pop() : t('r.pick')}
              </p>
              <p className="truncate font-mono text-[10.5px] text-ink-3">
                {autoLoaded && path ? t('r.autoload') : path || t('r.pick.hint')}
              </p>
            </div>
          </div>
        </Card>

        <Card className="flex flex-col gap-4 p-5">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-ink-1">{t('r.params')}</span>
            <button
              onClick={() => params && setParams({ ...params, ...Object.fromEntries(PARAM_SPECS.map((s) => [s.key, s.def])) })}
              disabled={locked}
              className="flex items-center gap-1.5 text-[11.5px] text-ink-3 transition-colors hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
              title={t('r.hint.reset')}
            >
              <RotateCcw size={12} strokeWidth={2} /> {t('r.reset')}
            </button>
          </div>
          {params && PARAM_SPECS.map((s) => (
            <div key={s.key} className="flex flex-col gap-1.5">
              <div className="flex items-baseline justify-between">
                <span className="text-[12px] text-ink-2">{t(s.label)}</span>
                <span className="font-mono text-[12px] text-primary">
                  {params[s.key]}
                  {s.unit ?? ''}
                </span>
              </div>
              {/* NOT the disabled attribute: WebView2 paints the thumb gray
                  for a disabled range input and then "forgets" to repaint it
                  on re-enable until the next hover. A controlled input plus
                  a change guard + pointer-events-none locks identically but
                  keeps the thumb's paint on OUR class change path. */}
              <input
                type="range"
                min={s.min}
                max={s.max}
                step={s.step}
                value={params[s.key]}
                aria-disabled={locked}
                tabIndex={locked ? -1 : undefined}
                onChange={(e) => {
                  if (locked) return;
                  setParams((p) => (p ? { ...p, [s.key]: Number(e.target.value) } : p));
                }}
                className={`h-1.5 w-full appearance-none rounded-full bg-line-2 accent-[var(--primary)] ${
                  locked ? 'pointer-events-none cursor-not-allowed opacity-40' : 'cursor-pointer'
                }`}
              />
            </div>
          ))}
          {/* LLM split is a one-shot ACTION, not a state: a button (with
              guidance copy) is the honest control for it. Colors stay
              ghost; the lift prop carries the shared hover dynamics
              (lift + shadow + icon pop) */}
          <div className="border-t border-line-1 pt-3">
            <Button
              className="w-full"
              variant="ghost"
              lift
              busy={busy && useLlm}
              disabled={!path || busy || player.playing}
              onClick={() => setUseLlm(true)}
            >
              <Wand2 size={14} strokeWidth={2} /> {t('r.llm.run')}
            </Button>
            <p className="mt-1.5 text-[10.5px] leading-relaxed text-ink-3">
              {t('r.llm.hint')}
            </p>
          </div>
        </Card>

        {/* natural height (column rule [&>*]:shrink-0) -- the saved-path
            link and any future sibling below stay scroll-reachable in a
            short window instead of being flex-squashed */}
        <div className="flex flex-col gap-2">
          {/* same look, same hover: they are siblings of one workflow --
              checkpoint often while refining (top), emit the artifact
              when done (bottom); order IS the hierarchy */}
          <Button
            variant="soft"
            className="group"
            disabled={!path || busy || saving || savingEdit || entries.length === 0}
            busy={savingEdit}
            onClick={saveEdit}
            title={t('r.hint.saveEdit')}
          >
            <Bookmark
              size={14}
              strokeWidth={2}
              className="transition-all duration-(--dur-in) group-hover:scale-110"
            />{' '}
            {t('r.saveEdit')}
          </Button>
          <Button
            variant="soft"
            className="group"
            disabled={!path || busy || saving || savingEdit || entries.length === 0}
            busy={saving}
            onClick={save}
          >
            <Save
              size={14}
              strokeWidth={2}
              className="transition-all duration-(--dur-in) group-hover:scale-110"
            />{' '}
            {t('r.save')}
          </Button>
        </div>
        {savedPath && (
          <button
            onClick={() =>
              revealPath(savedPath).catch((e) => toast('error', String(e).slice(0, 160)))
            }
            title={manualDirty ? t('r.hint.staleSrt') : t('r.hint.reveal')}
            className={`truncate text-left font-mono text-[11px] hover:underline ${
              manualDirty ? 'text-ink-3' : 'text-primary'
            }`}
          >
            {savedPath}
          </button>
        )}
        <AudioCard
          info={audioInfo}
          maiPath={path}
          editOpen={editOpen}
          onBound={setAudioInfo}
        />
      </div>

      {/* right: entry list */}
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="mb-3 flex h-8 items-center gap-4 text-[12px] text-ink-2">
          {busy && (
            <span className="flex items-center gap-1.5 text-primary">
              <Loader2 size={13} strokeWidth={2} className="animate-spin" />
              {useLlm ? t('r.generating.llm') : t('r.generating')}
            </span>
          )}
          {entries.length > 0 && (
            <>
              {/* no font-mono here: the mono stack has no CJK glyphs, so
                  the units (条/组对白) fell through to a system fallback
                  and stopped matching the UI font; body-wide tnum keeps
                  the digits aligned without it */}
              <span>{entries.length} {t('r.entries')}</span>
              <span className="text-primary">{dialogueCount} {t('r.dialogue')}</span>
            </>
          )}
          {manualDirty && (
            <span className="text-primary">
              {editOps > 0 ? tf('r.edited.badgeN', { n: editOps }) : t('r.edited.badge')}
            </span>
          )}
          {paramsDiffer && (
            <span className="text-warn">{t('r.recordParamsDiffer')}</span>
          )}
          {path && (
            <button
              onClick={restoreArchive}
              disabled={busy || player.playing}
              title={t('r.hint.restoreArchive')}
              className="text-primary transition-opacity hover:underline disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t('r.restoreArchive')}
            </button>
          )}
          {/* undo/redo cluster: sits WITH the workflow controls (a far
              ml-auto stranded it meters away from them in a maximized
              window); titles carry the shortcuts so the keys are
              discoverable without a manual. ink-2, not ink-3: these are
              always-visible icons, the faintest tier reads as disabled */}
          {path && (entries.length > 0 || undoStack.length > 0 || redoStack.length > 0) && (
            <span className="flex items-center gap-0.5">
              <button
                onClick={undo}
                disabled={!canUndo}
                title={t('r.undo')}
                className="grid h-6 w-6 place-items-center rounded text-ink-2 transition-opacity hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Undo2 size={13} strokeWidth={2} />
              </button>
              <button
                onClick={redo}
                disabled={!canRedo}
                title={t('r.redo')}
                className="grid h-6 w-6 place-items-center rounded text-ink-2 transition-opacity hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Redo2 size={13} strokeWidth={2} />
              </button>
            </span>
          )}
          {/* restore badges: ink-1 (the strongest tier) -- this is the
              "what am I looking at" record and it must not whisper at
              the far right of the row; the two are mutually exclusive
              (an LLM record carries no save time and vice versa) */}
          {restoredLlm && (
            <span className="ml-auto flex items-center gap-1.5 text-ink-1">
              <History size={12} strokeWidth={2} />
              {t('r.restoredLlm')}
            </span>
          )}
          {restoredAt && (
            <span className="ml-auto flex items-center gap-1.5 text-ink-1">
              <History size={12} strokeWidth={2} />
              {tf('r.restored', { time: restoredAt })}
            </span>
          )}
        </div>

        {/* regen banner: the undo stack makes every regeneration
            reversible; when it buried irreplaceable work, the button
            jumps straight back over the regen outputs in between */}
        <AnimatePresence>
          {banner && valuedFrame && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <div className="mb-3 flex items-center gap-3 rounded-(--radius-m) border border-primary/30 bg-primary-dim px-4 py-3 text-[12.5px] text-ink-1">
                <History size={14} strokeWidth={2} className="shrink-0 text-primary" />
                <span className="flex-1">{t('r.banner.regen')}</span>
                <Button variant="ghost" onClick={undoValued} disabled={histLock}>
                  <RotateCcw size={13} strokeWidth={2} />
                  {valuedFrame.editOps > 0
                    ? tf('r.banner.restoreEdits', { n: valuedFrame.editOps })
                    : valuedFrame.provenance === 'llm'
                      ? t('r.banner.restoreLlm')
                      : t('r.banner.restoreSaved')}
                </Button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <div className="glass min-h-0 flex-1 overflow-y-auto rounded-(--radius-m) p-2">
          {!path && (
            <div className="grid h-full place-items-center text-[13px] text-ink-3">
              {t('r.empty.noFile')}
            </div>
          )}
          {path && entries.length === 0 && !busy && (
            <div className="grid h-full place-items-center text-[13px] text-ink-3">
              {t('r.empty.noPreview')}
            </div>
          )}
          {entries.map((e, i) => {
            // limit check on live word refs (merges can exceed the params;
            // the BORDER channel belongs to this alarm alone)
            const dur = words[e.w0] && words[e.w1]
              ? words[e.w1].end - words[e.w0].start : 0;
            const chars = e.text.replace(/\s+/g, '').length;
            const over = params
              ? dur > params.max_duration || chars > params.max_chars
              : false;
            // this row's one-shot segment is what the player is inside
            const rowPlaying = i === segmentRow;
            // listen is unavailable when the row starts beyond the audio
            const beyond = player.durationMs > 0 && !!words[e.w0] &&
              words[e.w0].start * 1000 >= player.durationMs;
            return (
            <motion.div
              key={`${e.w0}-${e.w1}-${i}`}
              ref={(el) => { rowRefs.current[i] = el; }}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
              title={over ? t('r.overLimit') : undefined}
              data-row={i}
              onClick={(ev) => seekRow(i, ev)}
              className={`group relative mb-1.5 rounded-(--radius-s) border px-3.5 py-2.5 ${
                over
                  ? 'border-err/50'
                  : e.dialogue
                    ? 'border-primary/30 bg-primary-dim'
                    : 'border-transparent hover:border-line-1'
              } ${i === playingIdx ? 'bg-primary-dim ring-1 ring-primary/60' : ''} ${
                player.follow && audioInfo?.resolved ? 'cursor-pointer' : ''
              }`}
            >
              {/* playing-row marker: a bold bar straddling the left edge,
                  louder than the ring alone (user-specified affordance) */}
              {i === playingIdx && (
                <span className="absolute -left-[3px] top-1.5 bottom-1.5 w-[5px] rounded-full bg-primary" />
              )}
              {/* armed delete. Its state has to stay readable after the pointer
                  leaves the row (the tool cluster fades out with hover), and
                  two visual channels are already spoken for: the left edge is
                  the playhead and the border is the over-limit alarm. So it
                  takes the RIGHT edge plus a line along the bottom that drains
                  over exactly the arming window -- a countdown, not decoration. */}
              {delIdx === i && (
                <>
                  <span className="absolute -right-[3px] top-1.5 bottom-1.5 w-[5px] rounded-full bg-err" />
                  <ArmDrain className="bottom-0 left-3.5 right-3.5" />
                </>
              )}
              <div className="flex items-baseline gap-3">
                {/* edited marker lives on the INDEX channel (dot + bold
                    primary number) so it never fights the limit border */}
                <span className="inline-flex items-center gap-1 font-mono text-[10.5px]">
                  {e.edited && <span className="h-1.5 w-1.5 rounded-full bg-primary" />}
                  <span className={e.edited ? 'font-bold text-primary' : 'text-ink-3'}>
                    {String(i + 1).padStart(3, '0')}
                  </span>
                </span>
                <span className={`font-mono text-[11px] ${over ? 'text-err' : 'text-primary'}`}>
                  {words[e.w0] ? fmtEntryTime(words[e.w0].start) : '?'}
                  {' – '}
                  {words[e.w1] ? fmtEntryTime(words[e.w1].end) : '?'}
                </span>
                <span className="ml-auto flex items-center gap-1">
                  {/* listen stays visible while its own segment plays so the
                      stop affordance is reachable without hovering */}
                  <button
                    onClick={() => toggleListen(i)}
                    disabled={!audioInfo?.resolved || toolOpen || beyond}
                    className={`grid h-6 w-6 place-items-center rounded transition-opacity hover:bg-hover disabled:cursor-not-allowed disabled:opacity-40 ${
                      rowPlaying ? 'text-primary' : 'text-ink-3 opacity-0 group-hover:opacity-100 hover:text-primary'
                    }`}
                    title={rowPlaying ? t('r.listen.stop') : beyond ? t('r.listen.beyond') : t('r.listen')}
                  >
                    {rowPlaying
                      ? <Square size={10} strokeWidth={2} />
                      : <Play size={12} strokeWidth={2} />}
                  </button>
                  {/* entry actions: visible on hover, locked while playing
                      (they move the word indices the playhead maps onto) */}
                  <span className={`flex items-center gap-1 transition-opacity group-hover:opacity-100 ${
                    delIdx === i ? 'opacity-100' : 'opacity-0'
                  }`}>
                  <button
                    onClick={() => {
                      // one inline tool at a time: everything closes first
                      // (a draft open on ANOTHER row is committed rather than
                      // overwritten)
                      closeTool(null);
                      setEditingIdx(i);
                      setEditDraft(e.text);
                    }}
                    disabled={player.playing}
                    className="grid h-6 w-6 place-items-center rounded text-ink-3 hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
                    title={t('r.editText')}
                  >
                    <Pencil size={12} strokeWidth={2} />
                  </button>
                  <button
                    onClick={() => {
                      // scissors carry no draft, so other tools just close;
                      // they stay unreachable while an editor holds one
                      closeTool(i);
                      setSplitIdx(splitIdx === i ? null : i);
                    }}
                    /* structural tools are mutually exclusive: while ANY tool
                       is open only this row's own scissors (to close the panel)
                       and the pencil (the one safe switcher) can be clicked.
                       Hopping straight from an armed delete into a cut -- or
                       back -- is how a pending decision gets lost. */
                    disabled={player.playing || (toolOpen && splitIdx !== i)}
                    className={`grid h-6 w-6 place-items-center rounded hover:bg-hover disabled:cursor-not-allowed disabled:opacity-40 ${
                      splitIdx === i ? 'text-primary' : 'text-ink-3 hover:text-primary'
                    }`}
                    title={t('r.split')}
                  >
                    <Scissors size={12} strokeWidth={2} />
                  </button>
                  {/* two-click delete: arm, then confirm. The armed state is
                      drawn on the row itself (edge bar + draining line) and the
                      icon morphs with a single pop, so the first click reads as
                      "armed" rather than "nothing happened" */}
                  <button
                    onClick={() => {
                      if (delIdx === i) {
                        deleteEntry(i);
                      } else {
                        closeTool(null);     // arming closes every other tool
                        setDelIdx(i);
                        teachTwoStep(t('r.del.hint'));
                      }
                    }}
                    /* same mutual exclusion as the scissors: an armed delete
                       cannot be reached from an open split panel (nor from an
                       editor), only from a row with no tool open */
                    disabled={player.playing || (toolOpen && delIdx !== i)}
                    /* hover lives inside each branch: a shared hover:bg-hover
                       would override the armed tint exactly when the pointer
                       is on the button, i.e. just before the confirm click */
                    className={`grid h-6 w-6 place-items-center rounded disabled:cursor-not-allowed disabled:opacity-40 ${
                      delIdx === i
                        ? 'bg-err/10 text-err hover:bg-err/20'
                        : 'text-ink-3 hover:bg-hover hover:text-err'
                    }`}
                    title={delIdx === i ? t('r.del.confirm') : t('r.del')}
                  >
                    {/* keyed so arming replays the pop, and only arming */}
                    <motion.span
                      key={delIdx === i ? 'armed' : 'idle'}
                      initial={{ scale: 0.7 }}
                      animate={{ scale: [0.7, 1.3, 1] }}
                      transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
                      className="grid place-items-center"
                    >
                      {delIdx === i
                        ? <Check size={12} strokeWidth={2.5} />
                        : <Trash2 size={12} strokeWidth={2} />}
                    </motion.span>
                  </button>
                  {/* merge: same icon family, orientation = direction
                      (up = absorbed into previous, down = absorb next).
                      Locked while ANY inline tool is open, not just an editor:
                      a merge renumbers the entries an open draft or split panel
                      is anchored to, so it must not run around them. */}
                  {i > 0 && (
                    <button
                      onClick={() => mergeWithNext(i - 1)}
                      disabled={player.playing || toolOpen}
                      className="grid h-6 w-6 place-items-center rounded text-ink-3 hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
                      title={t('r.mergePrev')}
                    >
                      <Merge size={12} strokeWidth={2} />
                    </button>
                  )}
                  {i < entries.length - 1 && (
                    <button
                      onClick={() => mergeWithNext(i)}
                      disabled={player.playing || toolOpen}
                      className="grid h-6 w-6 place-items-center rounded text-ink-3 hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
                      title={t('r.mergeNext')}
                    >
                      <Merge size={12} strokeWidth={2} className="rotate-180" />
                    </button>
                  )}
                  </span>
                </span>
              </div>

              {editingIdx === i ? (
                <div className="mt-2 flex flex-col gap-2">
                  <textarea
                    autoFocus
                    value={editDraft}
                    onChange={(ev) => setEditDraft(ev.target.value)}
                    onKeyDown={(ev) => {
                      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) commitText(i);
                      if (ev.key === 'Escape') setEditingIdx(null);
                    }}
                    rows={Math.min(4, e.text.split('\n').length + 1)}
                    className="glass-input w-full resize-none px-3 py-2 text-[13px] leading-relaxed text-ink-1"
                  />
                  <div className="flex items-center gap-2">
                    <Button onClick={() => commitText(i)}>
                      <Check size={13} strokeWidth={2} /> {t('r.edit.ok')}
                    </Button>
                    <Button variant="ghost" onClick={() => setEditingIdx(null)}>
                      <X size={13} strokeWidth={2} /> {t('r.edit.cancel')}
                    </Button>
                    {/* clicking away COMMITS (a draft that was never committed
                        has no undo frame, so dropping it would be silent data
                        loss) -- cancelling stays an explicit choice */}
                    <span className="text-[10.5px] text-ink-3">{t('r.edit.hint')}</span>
                  </div>
                </div>
              ) : (
                /* affordance: the frame is invisible until hover, when a
                   dashed border + tint reveals the text is editable;
                   playing locks it (retext moves no indices, but a stale
                   draft would collide with the follow highlight) */
                <div
                  onClick={() => {
                    if (player.playing) return;
                    // click-anywhere-in-the-text entry (the dashed affordance on
                    // hover): it closes whatever tool is open first -- including
                    // the split panel on THIS row -- and commits a draft from
                    // another row instead of dropping it
                    closeTool(null);
                    setEditingIdx(i);
                    setEditDraft(e.text);
                  }}
                  title={t('r.editText')}
                  className={`mt-1 rounded-(--radius-s) border border-transparent px-2 py-1 transition-colors duration-(--dur-in) ${
                    player.playing
                      ? 'cursor-not-allowed'
                      : 'cursor-text hover:border-dashed hover:border-primary/60 hover:bg-hover'
                  }`}
                >
                  {e.text.split('\n').map((l, li) => (
                    <p key={li} className="text-[13px] leading-relaxed text-ink-1">
                      {l}
                    </p>
                  ))}
                </div>
              )}

              {/* split mode: TWO rows -- the granularity control, then the
                  chips. Sentence level is the default because a cut inside a
                  sentence is the rare case; character level highlights the
                  punctuation, which is what makes a wanted gap findable among
                  forty identical chips. */}
              {splitIdx === i && (
                <div data-noseek className="mt-2 border-t border-line-1 pt-2">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    {(['sentence', 'char'] as const).map((lv) => (
                      <button
                        key={lv}
                        onClick={() => setSplitLevel(lv)}
                        className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors duration-(--dur-in) ${
                          splitLevel === lv
                            ? 'border-primary bg-primary-dim text-primary'
                            : 'border-line-1 text-ink-2 hover:border-line-2'
                        }`}
                      >
                        {t(lv === 'sentence' ? 'r.split.by.sentence' : 'r.split.by.char')}
                      </button>
                    ))}
                    <span className="text-[10.5px] text-ink-3">
                      {t(splitLevel === 'sentence' ? 'r.split.hint' : 'r.split.hint.char')}
                    </span>
                  </div>

                  {splitLevel === 'sentence' ? (
                    <div className="flex flex-wrap items-center gap-y-1.5">
                      {(() => {
                        const spans = sentenceSpans(words, e.w0, e.w1);
                        return spans.map((sp, si) => (
                          <span key={si} className="flex items-center">
                            <span className="break-all rounded bg-sunken px-1.5 py-0.5 text-[11.5px] text-ink-1">
                              {joinWords(words.slice(sp.s, sp.t + 1))}
                            </span>
                            {si < spans.length - 1 && (
                              <button
                                onClick={() => splitAt(i, sp.t - e.w0)}
                                disabled={player.playing}
                                className="mx-1 grid h-5 w-4 place-items-center rounded text-ink-2 transition-colors hover:bg-primary-dim hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
                                title={t('r.split')}
                              >
                                <Scissors size={11} strokeWidth={2.2} />
                              </button>
                            )}
                          </span>
                        ));
                      })()}
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center gap-y-1.5">
                      {words.slice(e.w0, e.w1 + 1).map((w, wi) => (
                        <span key={wi} className="flex items-center">
                          {/* Every punctuation mark gets the SAME treatment: one
                              uniform rule ("punctuation = solid accent chip") is
                              scannable, whereas the splitter's internal cut
                              priority (final > comma > pause) is a decision rule
                              for the AUTOMATIC splitter -- a human picking a gap
                              does not rank candidates, and the glyph already
                              says which mark it is. Solid fill, not an alpha
                              tint: these chips sit on a glass card whose
                              transparency the user controls, and the 12% tint
                              this replaced composited to something LIGHTER than
                              the opaque chip beside it. */}
                          <span
                            className={`rounded px-1.5 py-0.5 text-[11.5px] ${
                              isPunctuation(w.text)
                                ? 'bg-primary font-semibold text-on-primary'
                                : 'bg-sunken text-ink-1'
                            }`}
                          >
                            {w.text}
                          </span>
                          {wi < e.w1 - e.w0 && (
                            <button
                              onClick={() => splitAt(i, wi)}
                              disabled={player.playing}
                              className="mx-0.5 grid h-4 w-3 place-items-center rounded text-ink-2 transition-colors hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
                              title={t('r.split')}
                            >
                              <Scissors size={10} strokeWidth={2.2} />
                            </button>
                          )}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </motion.div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
