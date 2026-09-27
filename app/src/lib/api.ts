// Thin client for the mai2srt backend (dev: 127.0.0.1:47613).
export const API_BASE = "http://127.0.0.1:47613";

/** FastAPI errors arrive as {"detail": ...} JSON -- surface just the message. */
async function errText(r: Response): Promise<string> {
  const raw = await r.text();
  try {
    const j = JSON.parse(raw);
    return typeof j.detail === "string" ? j.detail : raw;
  } catch {
    return raw;
  }
}

/** Retry a call while the backend is still coming up.
 *
 * The dev chain starts Vite and uvicorn side by side: Vite is ready in
 * well under a second while uvicorn needs ~1.4s to bind (measured), so the
 * webview can easily reach the window first. A one-shot initial fetch then
 * failed behind a "backend offline" toast and left the page EMPTY with no
 * way back short of a reload. Everything else is a real error and is
 * re-thrown after the last attempt. */
export async function withBackendRetry<T>(
  fn: () => Promise<T>,
  attempts = 8,
  delayMs = 600,
  // per-attempt timeout: a backend that ACCEPTS the socket but never
  // answers (wedged event loop) must count as a failed attempt, not hang
  // the retry loop forever
  attemptMs = 5000,
): Promise<T> {
  let last: unknown;
  for (let i = 0; i < attempts; i++) {
    try {
      return await Promise.race([
        fn(),
        new Promise<never>((_, rej) =>
          setTimeout(() => rej(new Error("backend timeout")), attemptMs)),
      ]);
    } catch (e) {
      last = e;
      if (i < attempts - 1) {
        await new Promise((r) => setTimeout(r, delayMs));
      }
    }
  }
  throw last;
}

export interface SystemInfo {
  version: string;
  python: string;
  data_dir: string;
  /** `source` says which copy won the resolution: the one that belongs to the
   *  app (shipped beside a frozen sidecar, or vendored under vendor/ffmpeg in
   *  a checkout), or the machine's own install found on PATH. */
  ffmpeg: { found: boolean; path: string | null; source: 'bundled' | 'path' | null };
  ffprobe: { found: boolean; path: string | null; source: 'bundled' | 'path' | null };
  chrome: { found: boolean; channel: string | null; path: string | null };
  browsers: Record<string, { found: boolean; path: string | null }>;
  browser: string; // configured preference: auto | chrome | msedge | chromium
  cookie_jar: boolean;
  busy: boolean;
}

/** provider preset shape served by GET /api/llm/presets */
export type LlmPreset = {
  name: string; protocol: string; auth_style: string;
  /** chip text, when it has to differ from `name`. The protocol-agnostic
   *  "custom" preset is the case in point: its stored name mentions OpenAI
   *  compatibility, but its button must not -- the user may pick any of the
   *  three API formats right below it. */
  label?: string;
  /** default endpoint per API format. The KEYS are exactly the formats the
   *  vendor actually serves -- the add-provider card offers nothing else
   *  (custom offers all three, with empty URLs to type into). `protocol`
   *  names the initially selected one. */
  urls: Record<string, string>;
};

/* ------------------------------------------------------- warm cache (SWR)
 *
 * The settings payloads are tiny and localhost-fast, yet a page still used
 * to mount with empty state and fill in a frame or two later -- visible as
 * components "materializing" inside the page slide-in. The shell therefore
 * primes this cache once at startup, EVERY getter refreshes it, and pages
 * seed their state SYNCHRONOUSLY from it so the first paint is already
 * complete. Pages that miss the cache (backend was down at startup) fall
 * back to the previous gate-until-loaded behaviour.
 */

const warm: {
  system: SystemInfo | null;
  session: SessionInfo | null;
  storage: StorageInfo | null;
  params: SubtitleParams | null;
  providers: Provider[] | null;
  presets: Record<string, LlmPreset> | null;
  active: ActiveLLM | null;
} = {
  system: null, session: null, storage: null, params: null,
  providers: null, presets: null, active: null,
};

export const warmSystem = () => warm.system;
export const warmSession = () => warm.session;
export const warmStorage = () => warm.storage;
export const warmParams = () => warm.params;
export const warmProviders = () => warm.providers;
export const warmPresets = () => warm.presets;
export const warmActive = () => warm.active;

/** startup prefetch of the payloads pages seed their first paint from.
 *  One-shot: React StrictMode double-invokes mount effects in dev, which
 *  used to fire every request twice on the console. Returns the shared
 *  settle promise so the boot gate (main.tsx) can wait for a COMPLETE
 *  first paint before handing the splash card over to the window. */
let primePromise: Promise<void> | null = null;

export function primeWarm(): Promise<void> {
  if (primePromise) return primePromise;
  // retry-aware: the packaged sidecar takes seconds to boot (PyInstaller
  // bootloader + import chain, longer under a first-launch AV scan), and
  // pages seed their first paint from this cache -- a cold-cache first
  // paint is exactly the "empty cards / missing presets" failure mode
  const getters: (() => Promise<unknown>)[] = [
    getSystem, getSession, getStorage, getSubtitleParams,
    llmProviders, llmPresets, llmActive,
  ];
  primePromise = Promise.all(
    getters.map((fn) => withBackendRetry(fn, 20, 800).catch(() => {})),
  ).then(() => {});
  return primePromise;
}

export interface SessionAccount {
  name: string;
  has_cookies: boolean;
  updated: string | null;
}

export interface SessionInfo {
  active: string | null; // null = no account registered yet (fresh install)
  accounts: SessionAccount[];
  /** whether finished transcriptions delete their playground conversation */
  delete_conversation: boolean;
}

/** the "leave no trace" toggle: on = delete the conversation after each
 *  successful transcription (historic default), off = keep it on the site */
export async function setDeleteMode(on: boolean): Promise<void> {
  const r = await fetch(`${API_BASE}/api/session/delete_mode`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ delete: on }),
  });
  if (!r.ok) throw new Error(await errText(r));
  // keep the warm cache coherent: the next mount paints this value
  if (warm.session) warm.session = { ...warm.session, delete_conversation: on };
}

export async function getSession(): Promise<SessionInfo> {
  const r = await fetch(`${API_BASE}/api/session`);
  if (!r.ok) throw new Error(`session: HTTP ${r.status}`);
  const data = await r.json();
  warm.session = data;
  return data;
}

export async function addAccount(name: string): Promise<string> {
  const r = await fetch(`${API_BASE}/api/session/accounts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) throw new Error(await errText(r));
  return (await r.json()).name;
}

export async function switchAccount(name: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/session/active`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!r.ok) throw new Error(await errText(r));
}

export async function deleteAccount(name: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/session/accounts/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(await errText(r));
}

export async function setBrowser(channel: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/browser`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel }),
  });
  if (!r.ok) throw new Error(await errText(r));
}

export interface JobInfo {
  id: string;
  kind: string;
  title: string;
  status: string;
}

export interface JobEvent {
  type: string; // stage | log | done | error | cancelled
  data: Record<string, unknown>;
  ts: number;
}

export interface DoneData {
  srt_path?: string;
  json_path?: string;
  words?: number;
  entries?: number;
  dialogue?: number;
  // None/absent = LLM off or fully applied; a string = some or all over-limit
  // runs fell back to the deterministic split (quality loss, worth a warning)
  llm_note?: string | null;
}

export async function getSystem(): Promise<SystemInfo> {
  const r = await fetch(`${API_BASE}/api/system`);
  if (!r.ok) throw new Error(`system: HTTP ${r.status}`);
  const data = await r.json();
  warm.system = data;
  return data;
}

/** Prerequisite key reported by /api/preflight (the UI owns the wording). */
export type PreflightKey = 'ffmpeg' | 'browser' | 'session' | 'llm';

export interface PreflightResult {
  ok: boolean;
  missing: PreflightKey[];
}

/** What a job is missing before it may start. Static + instant (no network
 *  probe): the live session check happens inside the job. */
export async function preflight(
  kind: 'run' | 'process',
  useLlm: boolean,
): Promise<PreflightResult> {
  const q = new URLSearchParams({ kind, use_llm: useLlm ? '1' : '0' });
  const r = await fetch(`${API_BASE}/api/preflight?${q}`);
  if (!r.ok) throw new Error(`preflight: HTTP ${r.status}`);
  return r.json();
}

export async function createJob(body: {
  kind: "run" | "process" | "login";
  audio_path?: string;
  mai_json_path?: string;
  use_llm?: boolean;
  params?: Record<string, number | boolean>;
}): Promise<JobInfo> {
  const r = await fetch(`${API_BASE}/api/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    throw new Error(await errText(r));
  }
  return (await r.json()).job;
}

export async function cancelJob(id: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/jobs/${id}/cancel`, { method: "POST" });
  if (!r.ok) throw new Error(`cancel: HTTP ${r.status}`);
}

/** Reveal a file selected in its containing folder (explorer /select). */
export async function revealPath(path: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/reveal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(await errText(r));
}

/** Subscribe to a job's SSE stream; returns a disposer. */
export function subscribeJob(
  jobId: string,
  handlers: {
    onStage?: (stage: string, detail: string) => void;
    onLog?: (line: string) => void;
    onDone?: (data: DoneData) => void;
    onError?: (message: string) => void;
    onCancelled?: () => void;
  },
): () => void {
  const es = new EventSource(`${API_BASE}/api/jobs/${jobId}/events`);
  es.addEventListener("stage", (e) => {
    const d = JSON.parse((e as MessageEvent).data);
    handlers.onStage?.(d.stage, d.detail ?? "");
  });
  es.addEventListener("log", (e) => {
    handlers.onLog?.(JSON.parse((e as MessageEvent).data).line);
  });
  es.addEventListener("done", (e) => {
    handlers.onDone?.(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.addEventListener("error", (e) => {
    if (e instanceof MessageEvent) {
      handlers.onError?.(JSON.parse(e.data).message ?? "unknown error");
      es.close();
    }
  });
  es.addEventListener("cancelled", () => {
    handlers.onCancelled?.();
    es.close();
  });
  return () => es.close();
}

/* ------------------------------------------------------------------ LLM */

export interface ModelMeta {
  id: string;
  context_window: number | null;
  max_output: number | null;
  reasoning: boolean;
  vision: boolean;
  efforts: string[] | null;
  source?: string;
  // where context_window/max_output came from: 'api' (provider reported them),
  // 'builtin' (matched a name pattern -- less trustworthy), or null/absent
  limits_source?: 'api' | 'builtin' | null;
}

export interface Provider {
  id: string;
  name: string;
  protocol: "openai" | "anthropic" | "gemini";
  base_url: string;
  api_key: string; // masked as "***" in responses
  auth_style: string;
  models: ModelMeta[];
}

export interface ActiveLLM {
  provider: string;
  model: string;
  effort: string | null;
}

export interface DiscoverDiff {
  added: ModelMeta[];
  removed: ModelMeta[];
  changed: { id: string; changes: Record<string, { from: unknown; to: unknown }> }[];
}

export async function llmPresets(): Promise<Record<string, LlmPreset>> {
  const data = (await fetch(`${API_BASE}/api/llm/presets`).then((r) => r.json())) as Record<string, LlmPreset>;
  warm.presets = data;
  return data;
}

export async function llmProviders(): Promise<Provider[]> {
  const providers = (await fetch(`${API_BASE}/api/llm/providers`).then((r) => r.json())).providers;
  warm.providers = providers;
  return providers;
}

export async function llmSaveProvider(p: Partial<Provider> & { id?: string }): Promise<Provider> {
  const r = await fetch(`${API_BASE}/api/llm/providers`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(p),
  });
  if (!r.ok) throw new Error(await errText(r));
  return (await r.json()).provider;
}

export async function llmDeleteProvider(id: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/llm/providers/${id}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`delete: HTTP ${r.status}`);
}

export async function llmDiscover(id: string): Promise<{ diff: DiscoverDiff; discovered: ModelMeta[] }> {
  const r = await fetch(`${API_BASE}/api/llm/providers/${id}/discover`, { method: "POST" });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

export async function llmApplyDiscovery(id: string, models: ModelMeta[]): Promise<void> {
  const r = await fetch(`${API_BASE}/api/llm/providers/${id}/discover/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ models, keep_manual: true }),
  });
  if (!r.ok) throw new Error(await errText(r));
}

export async function llmTest(
  id: string, model?: string, effort?: string,
): Promise<{ ok: boolean; model: string; sample: string }> {
  const p = new URLSearchParams();
  if (model) p.set('model', model);
  if (effort) p.set('effort', effort);
  const q = p.size ? `?${p}` : '';
  const r = await fetch(`${API_BASE}/api/llm/providers/${id}/test${q}`, { method: "POST" });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

export async function llmActive(): Promise<ActiveLLM | null> {
  const r = await fetch(`${API_BASE}/api/llm/active`);
  const data = await r.json();
  const active = data.provider ? (data as ActiveLLM) : null;
  warm.active = active;
  return active;
}

export async function llmSetActive(active: ActiveLLM): Promise<ActiveLLM> {
  const r = await fetch(`${API_BASE}/api/llm/active`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(active),
  });
  if (!r.ok) throw new Error(await errText(r));
  return (await r.json()).active;
}

/* ------------------------------------------------- subtitle params + refine */

export type SubtitleParams = Record<string, number>;

export interface PreviewWord {
  text: string;
  start: number;
  end: number;
  speaker: string | null;
}

/** one subtitle entry as a word-index range (structure editable client-side) */
export interface PreviewEntry {
  w0: number;
  w1: number;
  text: string;
  dialogue: boolean;
}

export interface EditRecord {
  version: number;
  source: string;
  saved_at: string;
  /** "llm" = transcription-time LLM segmentation (restore WITHOUT the
   *  manual-dirty conflict gate); "manual"/absent = user structure */
  origin?: string;
  params: SubtitleParams;
  entries: PreviewEntry[];
}

/** playback audio resolution for one mai.json (override > doc source).
 *  Video / exotic-audio sources are PENDING until their track is
 *  extracted into the cache (prepareAudio does that). */
export interface AudioInfo {
  source: string | null;
  override: string | null;
  /** existing-on-disk playable path (or the extraction cache); null =
   *  nothing playable yet */
  resolved: string | null;
  /** true when the source exists but needs extraction before playback */
  pending: boolean;
  /** the file awaiting extraction (video/exotic audio), else null */
  pending_path: string | null;
  /** the would-be path when nothing exists (display + rebinding target) */
  missing: string | null;
  /** the TRANSCRIPT's own duration (the .mai.json's `duration` field) -- NOT a
   *  probe of the audio file. The audio card compares it against the media the
   *  player actually loaded, and a difference beyond max(1s, 1%) is what raises
   *  the "playback may drift" warning. */
  duration_s: number;
}

export interface PreviewResponse {
  words: PreviewWord[];
  entries: PreviewEntry[];
  entries_count: number;
  dialogue: number;
  edit?: EditRecord;
  /** null/absent = LLM ok or not requested; "unavailable" = no endpoint;
   *  anything else = splitter failure message (fallback results returned) */
  llm_note?: string | null;
  audio?: AudioInfo;
}

/** ranged stream URL for one local audio file (seeking needs the 206s) */
export function audioUrl(path: string): string {
  return `${API_BASE}/api/audio?path=${encodeURIComponent(path)}`;
}

/** bind (or clear, with null) the playback audio for one mai.json.
 *  Videos are accepted too -- they play through the extraction cache. */
export async function setAudioBinding(
  maiJsonPath: string,
  audioPath: string | null,
): Promise<AudioInfo> {
  const r = await fetch(`${API_BASE}/api/audio_binding`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mai_json_path: maiJsonPath, audio_path: audioPath }),
  });
  if (!r.ok) throw new Error(await errText(r));
  return (await r.json()).audio;
}

/** extract the audio track of a PENDING source (first call pays the
 *  remux/transcode; every later open hits the cache) */
export async function prepareAudio(maiJsonPath: string): Promise<AudioInfo> {
  const r = await fetch(`${API_BASE}/api/audio/prepare`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mai_json_path: maiJsonPath }),
  });
  if (!r.ok) throw new Error(await errText(r));
  return (await r.json()).audio;
}

/* ------------------------------------------------------- audio-extraction cache */

/** one extracted track in the playback cache */
export interface AudioCacheEntry {
  file: string;
  source: string | null;
  size: number;
  mtime: string;
}

export interface AudioCacheInfo {
  dir: string;
  entries: AudioCacheEntry[];
  total_bytes: number;
  removed?: number;
}

export async function getAudioCache(): Promise<AudioCacheInfo> {
  const r = await fetch(`${API_BASE}/api/audio_cache`);
  if (!r.ok) throw new Error(`audio cache: HTTP ${r.status}`);
  return r.json();
}

/** batch-remove cached tracks (plain file names inside the cache dir) */
export async function deleteAudioCache(files: string[]): Promise<AudioCacheInfo> {
  const r = await fetch(`${API_BASE}/api/audio_cache/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files }),
  });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

export async function clearAudioCache(): Promise<AudioCacheInfo> {
  const r = await fetch(`${API_BASE}/api/audio_cache/clear`, { method: "POST" });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

export async function getSubtitleParams(): Promise<{ params: SubtitleParams; last_mai_json: string | null }> {
  const r = await fetch(`${API_BASE}/api/params`);
  if (!r.ok) throw new Error(`params: HTTP ${r.status}`);
  const data = await r.json();
  warm.params = data.params;
  return data;
}

export async function setSubtitleParams(params: SubtitleParams): Promise<SubtitleParams> {
  const r = await fetch(`${API_BASE}/api/params`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params }),
  });
  if (!r.ok) throw new Error(await errText(r));
  const echoed = (await r.json()).params as SubtitleParams;
  // the refine page writes these; the transcribe page reads them on mount
  warm.params = echoed;
  return echoed;
}

export async function previewEntries(
  maiJsonPath: string,
  params: SubtitleParams,
  useLlm: boolean,
): Promise<PreviewResponse> {
  const r = await fetch(`${API_BASE}/api/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mai_json_path: maiJsonPath, use_llm: useLlm, params }),
  });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

/** serialize user-edited entries -> .srt (+ .edit.json record on the backend).
 * `nameSuffix` localizes the refined-output marker (_精修 / _refined) so
 * the file distinguishes itself from the initial transcription result. */
export async function renderSrt(
  maiJsonPath: string,
  entries: PreviewEntry[],
  params: SubtitleParams,
  nameSuffix?: string,
): Promise<{ srt_path: string; entries: number; dialogue: number }> {
  const r = await fetch(`${API_BASE}/api/render`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      mai_json_path: maiJsonPath, entries, params, name_suffix: nameSuffix,
    }),
  });
  if (!r.ok) throw new Error(await errText(r));
  return r.json();
}

/** archive the session entries as a manual edit record WITHOUT rendering
 *  the .srt (refine file-switch auto-save: switching must never lose work) */
export async function saveEditRecord(
  maiJsonPath: string,
  entries: PreviewEntry[],
  params: SubtitleParams,
): Promise<void> {
  const r = await fetch(`${API_BASE}/api/edit_record`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mai_json_path: maiJsonPath, entries, params }),
  });
  if (!r.ok) throw new Error(await errText(r));
}

/* ---------------------------------------------------------------- storage */

/** one managed .mai.json project inside the storage library */
export interface StorageProject {
  name: string;
  path: string;
  size: number;
  mtime: string | null;
  has_edit: boolean;
  audio: {
    resolved: string | null;
    missing: string | null;
    /** video source, cache not extracted yet (read-only status) */
    pending: boolean;
    /** the effective source (override > doc source) is a video file */
    video: boolean;
  };
}

export interface StorageInfo {
  dir: string;
  default_dir: string;
  /** false until the first write creates the folder (fresh install) --
   *  dialogs must not default into a non-existent path */
  exists: boolean;
  projects: StorageProject[];
  total_bytes: number;
  /** present on import responses */
  imported?: number;
  skipped?: string[];
  /** present on move responses */
  moved?: number;
}

export async function getStorage(): Promise<StorageInfo> {
  const r = await fetch(`${API_BASE}/api/storage`);
  if (!r.ok) throw new Error(`storage: HTTP ${r.status}`);
  const data = await r.json();
  warm.storage = data;
  return data;
}

/** switch the storage location; move=true relocates existing projects */
export async function setStorageDir(dir: string, move: boolean): Promise<StorageInfo> {
  const r = await fetch(`${API_BASE}/api/storage/dir`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dir, move }),
  });
  if (!r.ok) throw new Error(await errText(r));
  const data = await r.json();
  warm.storage = data;          // keep dialogs seeding from the NEW dir
  return data;
}

export async function importProjects(
  paths: string[],
  mode: 'copy' | 'move',
): Promise<StorageInfo> {
  const r = await fetch(`${API_BASE}/api/storage/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths, mode }),
  });
  if (!r.ok) throw new Error(await errText(r));
  const data = await r.json();
  warm.storage = data;
  return data;
}

export async function deleteProject(path: string): Promise<StorageInfo> {
  const r = await fetch(
    `${API_BASE}/api/storage/project?path=${encodeURIComponent(path)}`,
    { method: 'DELETE' },
  );
  if (!r.ok) throw new Error(await errText(r));
  const data = await r.json();
  warm.storage = data;
  return data;
}

/** make a storage project the refine target; navigate to refine right
 * after -- the page auto-loads last_mai_json on mount */
export async function openProject(path: string): Promise<void> {
  const r = await fetch(`${API_BASE}/api/storage/open`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(await errText(r));
}
