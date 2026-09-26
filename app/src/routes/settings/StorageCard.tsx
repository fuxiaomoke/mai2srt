import { useCallback, useEffect, useState } from 'react';
import {
  FileJson, FolderOpen, HardDrive, Import, SlidersHorizontal, Trash2, X,
} from 'lucide-react';
import { open } from '@tauri-apps/plugin-dialog';
import { ConfirmIconButton, Button, Card, StatusDot, useToast } from '../../components/ui';
import {
  clearAudioCache, deleteAudioCache, deleteProject, getAudioCache,
  getStorage, importProjects, openProject, revealPath, setStorageDir,
  warmStorage,
  type AudioCacheInfo, type StorageInfo, type StorageProject,
} from '../../lib/api';
import { useI18n } from '../../lib/i18n';

/** 1024-based size formatting for the totals row and project lines */
function fmtSize(n: number): string {
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

function fmtMtime(iso: string | null): string {
  return iso ? iso.slice(0, 16).replace('T', ' ') : '';
}

/* -------------------------------------------------------- storage manager */

export function StorageCard() {
  const { t, tf } = useI18n();
  const toast = useToast();
  const [info, setInfo] = useState<StorageInfo | null>(warmStorage);
  // seeded from the startup cache -> first paint is already complete;
  // a cold cache (backend was down) stays unpainted until the fetch
  // settles, so the "storage is empty" placeholder can never flash
  const [ready, setReady] = useState(() => warmStorage() !== null);
  // extraction-cache manager: checkbox selection + batch delete/clear
  const [cache, setCache] = useState<AudioCacheInfo | null>(null);
  const [selCache, setSelCache] = useState<Set<string>>(new Set());
  const [confirmClear, setConfirmClear] = useState(false);
  const [cacheBusy, setCacheBusy] = useState(false);
  // same incremental reveal as the project list above: 6 rows, +3 per click
  const [cacheVisible, setCacheVisible] = useState(6);
  // a picked folder waiting for the move/switch-only decision
  const [pendingDir, setPendingDir] = useState<string | null>(null);
  // picked files waiting for the copy/move decision
  const [importPick, setImportPick] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  // incremental reveal: the list opens at 6 rows and each click on the
  // "show more" link unwraps 3 more (user preference over expand-all)
  const [visible, setVisible] = useState(6);

  const refresh = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([getStorage(), getAudioCache()]);
      setInfo(s);
      setCache(c);
    } catch {
      /* backend offline */
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // self-heal for out-of-app changes: the user may delete an audio file
  // in Explorer (revealed from here) while this page is open -- a focus
  // refresh keeps the audio statuses honest without a manual revisit
  useEffect(() => {
    const onFocus = () => refresh();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [refresh]);

  function toggleCacheSel(file: string) {
    setSelCache((s) => {
      const next = new Set(s);
      if (next.has(file)) next.delete(file);
      else next.add(file);
      return next;
    });
  }

  async function deleteSelCache() {
    if (!selCache.size) return;
    setCacheBusy(true);
    try {
      const r = await deleteAudioCache([...selCache]);
      setCache(r);
      setSelCache(new Set());
      toast('ok', tf('s.cache.toast.deleted', { n: r.removed ?? 0 }));
      // project audio statuses DEPEND on the cache: deleting an
      // extraction flips its video card back to "pending extraction" --
      // the storage list must refetch, not wait for a manual revisit
      refresh();
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    } finally {
      setCacheBusy(false);
    }
  }

  /** two-step clear-all: first click arms the confirm label */
  function askClearCache() {
    if (confirmClear) {
      setConfirmClear(false);
      setCacheBusy(true);
      clearAudioCache()
        .then((r) => {
          setCache(r);
          setSelCache(new Set());
          toast('ok', t('s.cache.toast.cleared'));
          // same cache -> project-status dependency as deleteSelCache
          refresh();
        })
        .catch((e) => toast('error', String(e).slice(0, 160)))
        .finally(() => setCacheBusy(false));
    } else {
      setConfirmClear(true);
      window.setTimeout(
        () => setConfirmClear((cur) => (cur ? false : cur)), 3000);
    }
  }

  async function pickDir() {
    const p = await open({ multiple: false, directory: true });
    if (typeof p === 'string') setPendingDir(p);
  }

  async function applyDir(move: boolean) {
    if (!pendingDir) return;
    setBusy(true);
    try {
      const r = await setStorageDir(pendingDir, move);
      setInfo(r);
      setPendingDir(null);
      if (move) toast('ok', tf('s.storage.toast.dirMoved', { n: r.moved ?? 0 }));
      else toast('ok', t('s.storage.toast.dirSet'));
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    } finally {
      setBusy(false);
    }
  }

  async function pickImport() {
    const ps = await open({
      multiple: true,
      filters: [{ name: 'mai.json', extensions: ['mai.json', 'json'] }],
    });
    if (Array.isArray(ps) && ps.length) setImportPick(ps);
  }

  async function applyImport(mode: 'copy' | 'move') {
    if (!importPick) return;
    setBusy(true);
    try {
      const r = await importProjects(importPick, mode);
      setInfo(r);
      setImportPick(null);
      if (r.imported) toast('ok', tf('s.storage.toast.imported', { n: r.imported }));
      if (r.skipped?.length) toast('warn', tf('s.storage.toast.skipped', { n: r.skipped.length }));
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    } finally {
      setBusy(false);
    }
  }

  /** the confirm step lives in ConfirmIconButton: it owns the arming, the
   *  countdown line and the disarm, so every destructive control in the app
   *  behaves the same way */
  function removeProject(p: StorageProject) {
    setBusy(true);
    deleteProject(p.path)
      .then((r) => {
        setInfo(r);
        toast('ok', tf('s.storage.toast.deleted', { name: p.name }));
      })
      .catch((e) => toast('error', String(e).slice(0, 160)))
      .finally(() => setBusy(false));
  }

  async function openInRefine(p: StorageProject) {
    try {
      await openProject(p.path);
      window.dispatchEvent(new CustomEvent('mai2srt:nav', { detail: 'refine' }));
    } catch (e) {
      toast('error', tf('s.storage.toast.openFailed', { err: String(e).slice(0, 120) }));
    }
  }

  const projects = info?.projects ?? [];
  if (!ready) return null;

  return (
    <Card className="flex flex-col gap-4 p-5">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-[13px] font-semibold text-ink-1">
          <HardDrive size={14} strokeWidth={2} className="text-ink-3" />
          {t('s.storage')}
        </span>
        {info && projects.length > 0 && (
          <span className="shrink-0 text-[11px] text-ink-3">
            {tf('s.storage.total', { n: projects.length, size: fmtSize(info.total_bytes) })}
          </span>
        )}
      </div>
      <p className="text-[11px] leading-relaxed text-ink-3">{t('s.storage.hint')}</p>

      {/* location row: all folder-level actions live together */}
      <div className="flex items-center gap-3">
        <span className="shrink-0 text-[12.5px] text-ink-2">{t('s.storage.dir')}</span>
        <span
          className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-3"
          title={info?.dir}
        >
          {info?.dir}
        </span>
        <Button variant="ghost" lift onClick={pickDir} disabled={busy}>
          <FolderOpen size={13} strokeWidth={2} /> {t('s.storage.change')}
        </Button>
        <Button
          variant="ghost" lift disabled={!info}
          onClick={() => info && revealPath(info.dir).catch((e) => toast('error', String(e).slice(0, 160)))}
        >
          {t('s.storage.open')}
        </Button>
        <Button variant="ghost" lift onClick={pickImport} disabled={busy}>
          <Import size={13} strokeWidth={2} /> {t('s.storage.import')}
        </Button>
      </div>

      {/* folder picked -> move or just switch */}
      {pendingDir && (
        <div className="flex flex-wrap items-center gap-2 rounded-(--radius-m) border border-primary/40 bg-primary-dim px-3 py-2">
          <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2" title={pendingDir}>
            {tf('s.storage.pending', { dir: pendingDir })}
          </span>
          <Button lift busy={busy} onClick={() => applyDir(true)}>{t('s.storage.move')}</Button>
          <Button variant="ghost" lift busy={busy} onClick={() => applyDir(false)}>{t('s.storage.switchOnly')}</Button>
          <button
            onClick={() => setPendingDir(null)}
            className="grid h-7 w-7 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-ink-1"
            title={t('s.cancel')}
          >
            <X size={14} strokeWidth={2} />
          </button>
        </div>
      )}

      {/* files picked -> move in or copy in */}
      {importPick && (
        <div className="flex flex-wrap items-center gap-2 rounded-(--radius-m) border border-primary/40 bg-primary-dim px-3 py-2">
          <span className="min-w-0 flex-1 truncate text-[12px] text-ink-2">
            {tf('s.storage.import.bar', { n: importPick.length })}
          </span>
          <Button lift busy={busy} onClick={() => applyImport('move')}>{t('s.storage.import.move')}</Button>
          <Button variant="ghost" lift busy={busy} onClick={() => applyImport('copy')}>{t('s.storage.import.copy')}</Button>
          <button
            onClick={() => setImportPick(null)}
            className="grid h-7 w-7 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-ink-1"
            title={t('s.cancel')}
          >
            <X size={14} strokeWidth={2} />
          </button>
        </div>
      )}

      {/* project list */}
      {projects.length === 0 ? (
        <div className="rounded-(--radius-m) border border-dashed border-line-2 px-4 py-6 text-center">
          <p className="text-[12.5px] text-ink-3">{t('s.storage.empty')}</p>
          <p className="mt-1 text-[11px] text-ink-3">{t('s.storage.empty.hint')}</p>
        </div>
      ) : (
        <>
          <div className="flex max-h-[360px] flex-col gap-1.5 overflow-y-auto pr-1">
          {projects.slice(0, visible).map((p) => (
            <div
              key={p.path}
              className="flex items-center gap-3 rounded-(--radius-m) border border-line-1 px-3 py-2 transition-colors duration-(--dur-in) hover:border-line-2"
            >
              <FileJson size={15} strokeWidth={2} className="shrink-0 text-ink-3" />
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-2 truncate text-[12.5px] font-semibold text-ink-1">
                  <span className="truncate" title={p.path}>{p.name}</span>
                  {p.has_edit && (
                    <span className="shrink-0 rounded-full bg-sunken px-1.5 py-px text-[9.5px] font-medium text-ink-3">
                      {t('s.storage.hasEdit')}
                    </span>
                  )}
                </p>
                <p className="truncate text-[10.5px] text-ink-3">
                  {fmtMtime(p.mtime)} · {fmtSize(p.size)}
                </p>
              </div>
              {/* audio availability (video sources resolve via the cache) */}
              <span
                className="flex shrink-0 items-center gap-1.5 text-[11px] text-ink-3"
                title={p.audio.resolved ?? p.audio.missing ?? undefined}
              >
                <StatusDot
                  ok={!!p.audio.resolved}
                  warn={!p.audio.resolved && p.audio.pending}
                />
                {p.audio.resolved
                  ? (p.audio.video ? t('s.storage.audioVideo') : t('s.storage.audioOk'))
                  : p.audio.pending ? t('s.storage.audioVideoPending')
                  : p.audio.missing ? t('s.storage.audioMissing')
                  : t('s.storage.audioNone')}
              </span>
              <div className="flex shrink-0 items-center gap-1">
                <Button variant="ghost" lift onClick={() => openInRefine(p)} title={t('s.storage.openInRefine')}>
                  <SlidersHorizontal size={13} strokeWidth={2} />
                </Button>
                <button
                  onClick={() => revealPath(p.path).catch((e) => toast('error', String(e).slice(0, 160)))}
                  className="grid h-8 w-8 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-ink-1"
                  title={t('s.hint.reveal')}
                >
                  <FolderOpen size={13} strokeWidth={2} />
                </button>
                <ConfirmIconButton
                  icon={<Trash2 size={13} strokeWidth={2} />}
                  iconSize={13}
                  onConfirm={() => removeProject(p)}
                  title={t('s.storage.delete')}
                  confirmTitle={t('s.storage.deleteConfirm')}
                  hint={t('s.deleteHint')}
                />
              </div>
            </div>
          ))}
          </div>
          {visible < projects.length && (
            <button
              onClick={() => setVisible((v) => v + 3)}
              className="self-start text-[11.5px] text-ink-3 transition-colors hover:text-primary"
            >
              {tf('s.storage.more', { n: projects.length - visible })}
            </button>
          )}
        </>
      )}

      {/* extracted-audio cache: batch-managed (user request). Derived
          data -- deleting anything only costs the next first-play. */}
      <div className="border-t border-line-1 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-[12px] font-semibold text-ink-2">
            {t('s.cache')}
            {cache && cache.entries.length > 0 && (
              <span className="font-mono text-[10.5px] font-normal text-ink-3">
                {cache.entries.length} · {fmtSize(cache.total_bytes)}
              </span>
            )}
          </span>
          {cache && cache.entries.length > 0 && (
            <div className="flex items-center gap-1.5">
              <Button
                variant="ghost" lift disabled={cacheBusy}
                onClick={() => setSelCache((s) =>
                  s.size >= cache.entries.length
                    ? new Set() : new Set(cache.entries.map((e) => e.file)))}
              >
                {t('s.cache.selectAll')}
              </Button>
              <Button
                variant="ghost" lift disabled={!selCache.size || cacheBusy}
                onClick={deleteSelCache}
              >
                <Trash2 size={13} strokeWidth={2} />
                {tf('s.cache.deleteSel', { n: selCache.size })}
              </Button>
              <Button
                variant="ghost" lift disabled={cacheBusy}
                onClick={askClearCache}
                className={confirmClear ? 'text-err' : ''}
              >
                {confirmClear ? t('s.cache.confirmClear') : t('s.cache.clear')}
              </Button>
            </div>
          )}
        </div>
        <p className="mt-1 text-[11px] leading-relaxed text-ink-3">
          {t('s.cache.hint')}
        </p>
        {cache && cache.entries.length > 0 && (
          <>
            <div className="mt-2 flex max-h-[200px] flex-col gap-1 overflow-y-auto pr-1">
              {cache.entries.slice(0, cacheVisible).map((e) => {
                // card name: the SOURCE media's stem with the cached
                // track's real extension (abc.mp4 -> abc.m4a) -- the entry
                // IS the extracted audio, so the name never shows the
                // video extension. Hover + reveal use the REAL on-disk
                // path (hash-named track inside the cache dir).
                const srcName = e.source?.split(/[\\/]/).pop() ?? null;
                const ext = (e.file.split('.').pop() ?? '').toLowerCase();
                const label = srcName && ext
                  ? srcName.replace(/\.[^.]+$/, '') + '.' + ext
                  : (srcName ?? e.file);
                const realPath = `${cache.dir}\\${e.file}`;
                return (
                  <div
                    key={e.file}
                    className="flex items-center gap-2.5 rounded-(--radius-s) border border-line-1 px-3 py-1.5 transition-colors duration-(--dur-in) hover:border-line-2"
                  >
                    <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5">
                      <input
                        type="checkbox"
                        checked={selCache.has(e.file)}
                        onChange={() => toggleCacheSel(e.file)}
                        className="h-3.5 w-3.5 shrink-0 cursor-pointer accent-[var(--primary)]"
                      />
                      <span
                        className="min-w-0 flex-1 truncate text-[12px] text-ink-1"
                        title={realPath}
                      >
                        {label}
                      </span>
                    </label>
                    <span className="shrink-0 font-mono text-[10.5px] text-ink-3">
                      {fmtSize(e.size)}
                    </span>
                    <button
                      onClick={() => revealPath(realPath)
                        .catch((err) => toast('error', String(err).slice(0, 160)))}
                      className="grid h-7 w-7 shrink-0 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-ink-1"
                      title={t('s.hint.reveal')}
                    >
                      <FolderOpen size={13} strokeWidth={2} />
                    </button>
                  </div>
                );
              })}
            </div>
            {cacheVisible < cache.entries.length && (
              <button
                onClick={() => setCacheVisible((v) => v + 3)}
                className="mt-1.5 self-start text-[11.5px] text-ink-3 transition-colors hover:text-primary"
              >
                {tf('s.storage.more', { n: cache.entries.length - cacheVisible })}
              </button>
            )}
          </>
        )}
      </div>
    </Card>
  );
}
