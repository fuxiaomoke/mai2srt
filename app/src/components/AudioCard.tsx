/**
 * AudioCard — the refine page's playback card (left column, under the save
 * buttons). Owns the transport UI; the playback kernel lives in lib/player.
 *
 * Boundary policy (per the accepted design):
 *  - audio shorter/longer than the transcript -> a warning line, playback
 *    still allowed (差分音轨 is a legitimate, deliberate binding);
 *  - rebinding and restoring the default are locked while playing;
 *  - play is locked while a text editor is open (the draft must not be
 *    silently stranded by the playback lock).
 */
import { useState } from 'react';
import { open } from '@tauri-apps/plugin-dialog';
import {
  AlertTriangle, FolderOpen, Loader2, Music2, Pause, Play, RotateCcw,
} from 'lucide-react';
import { Button, Card, Select, Toggle, useToast } from './ui';
import { useI18n } from '../lib/i18n';
import { setAudioBinding, type AudioInfo } from '../lib/api';
import {
  RATES, fmtClock, playerSeek, playerSetFollow, playerSetRate,
  playerToggle, usePlayer,
} from '../lib/player';

const AUDIO_EXTS = ['mp3', 'wav', 'm4a', 'aac', 'ogg', 'oga', 'flac', 'opus'];
const VIDEO_EXTS = ['mp4', 'mkv', 'webm', 'mov', 'm4v', 'avi', 'wmv', 'ts', 'flv'];

export function AudioCard({
  info,
  maiPath,
  editOpen,
  onBound,
}: {
  info: AudioInfo | null;
  maiPath: string;
  editOpen: boolean;
  onBound: (info: AudioInfo) => void;
}) {
  const { t, tf } = useI18n();
  const toast = useToast();
  const player = usePlayer();
  const [binding, setBinding] = useState(false);

  if (!info) return null;

  // the display name is the USER-FACING source (binding / transcription
  // source): for a video source `resolved` is the extraction-cache HASH
  // file, which reads as garbage on the card title
  const displayPath = info.override ?? info.source ?? info.resolved ?? info.missing;
  const name = displayPath ? (displayPath.split(/[\\/]/).pop() ?? displayPath) : '';
  // beyond this the timestamps are advisory, not an error: a 差分音轨 or an
  // edited master legitimately differs from the transcribed source
  const mismatch =
    player.durationMs > 0 && info.duration_s > 0 &&
    Math.abs(player.durationMs / 1000 - info.duration_s) >
      Math.max(1, info.duration_s * 0.01);
  const playDisabled = !info.resolved || editOpen || binding;
  // the seek bar stays live DURING playback (dragging it is the point)
  const seekLocked = playDisabled && !player.playing;

  async function bind(path: string | null) {
    setBinding(true);
    try {
      onBound(await setAudioBinding(maiPath, path));
    } catch (e) {
      toast('error', tf('r.audio.bindFailed', { err: String(e).slice(0, 120) }));
    } finally {
      setBinding(false);
    }
  }

  async function pickAudio() {
    const sel = await open({
      multiple: false,
      // videos bind too (user decision): they play through the
      // extraction cache like any other video source
      filters: [
        { name: 'audio', extensions: AUDIO_EXTS },
        { name: 'video', extensions: VIDEO_EXTS },
      ],
    });
    if (typeof sel === 'string') await bind(sel);
  }

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-2.5">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-(--radius-s) bg-primary-dim text-primary">
          <Music2 size={15} strokeWidth={1.8} />
        </span>
        <div className="min-w-0 flex-1">
          <p
            className="truncate text-[12.5px] font-semibold text-ink-1"
            title={displayPath ?? undefined}
          >
            {name || t('r.audio')}
          </p>
          {(player.playing || editOpen) && (
            <p className="truncate text-[10.5px] text-ink-3">
              {player.playing ? t('r.audio.locked') : t('r.audio.editOpen')}
            </p>
          )}
        </div>
        {info.override && (
          <button
            onClick={() => bind(null)}
            disabled={binding || player.playing}
            title={t('r.audio.reset')}
            className="grid h-7 w-7 shrink-0 place-items-center rounded text-ink-3 transition-colors hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
          >
            <RotateCcw size={12} strokeWidth={2} />
          </button>
        )}
        {displayPath && (
          <button
            onClick={pickAudio}
            disabled={binding || player.playing}
            title={t('r.audio.change')}
            className="grid h-7 w-7 shrink-0 place-items-center rounded text-ink-3 transition-colors hover:bg-hover hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
          >
            <FolderOpen size={13} strokeWidth={2} />
          </button>
        )}
      </div>

      {info.resolved ? (
        <>
          <div className="flex items-center gap-2.5">
            {/* the play button is the primary control: largest element on
                the card; rate/select demoted to compact so nothing competes */}
            <button
              onClick={playerToggle}
              disabled={playDisabled}
              title={editOpen && !player.playing ? t('r.audio.editOpen') : undefined}
              className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-primary text-on-primary transition-all duration-(--dur-in) hover:-translate-y-0.5 hover:shadow-[var(--shadow-1)] active:translate-y-0 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:translate-y-0 disabled:hover:shadow-none"
            >
              {player.playing ? (
                <Pause size={14} strokeWidth={2} />
              ) : (
                <Play size={14} strokeWidth={2} className="ml-0.5" />
              )}
            </button>
            <span className="shrink-0 font-mono text-[11px] text-ink-2">
              {fmtClock(player.currentMs)}
            </span>
            {/* no disabled attribute (WebView2 sticks the grayed thumb until
                re-hover); locked = controlled value + guard + no pointer */}
            <input
              type="range"
              min={0}
              max={Math.max(1, Math.round(player.durationMs))}
              step={50}
              value={Math.round(Math.min(player.currentMs, player.durationMs || player.currentMs))}
              aria-disabled={seekLocked}
              tabIndex={seekLocked ? -1 : undefined}
              onChange={(e) => {
                if (seekLocked) return;
                playerSeek(Number(e.target.value));
              }}
              className={`h-1.5 w-full min-w-0 flex-1 appearance-none rounded-full bg-line-2 accent-[var(--primary)] ${
                seekLocked ? 'pointer-events-none cursor-not-allowed opacity-40' : 'cursor-pointer'
              }`}
            />
            <span className="shrink-0 font-mono text-[11px] text-ink-3">
              {fmtClock(player.durationMs)}
            </span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <Select
              size="sm"
              className="w-[72px] shrink-0"
              value={String(player.rate)}
              onChange={(v) => playerSetRate(Number(v))}
              options={RATES.map((r) => ({ value: String(r), label: `${r}x` }))}
            />
            <Toggle
              checked={player.follow}
              onChange={playerSetFollow}
              label={t('r.audio.follow')}
            />
          </div>
        </>
      ) : info.pending ? (
        <p className="flex items-center gap-2 text-[11px] text-ink-2">
          <Loader2 size={12} strokeWidth={2} className="animate-spin shrink-0" />
          {t('r.audio.preparing')}
        </p>
      ) : info.missing ? (
        <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-warn">
          <AlertTriangle size={12} strokeWidth={2} className="mt-0.5 shrink-0" />
          <span className="min-w-0 break-all">
            {t('r.audio.missing')}：{info.missing}
          </span>
        </p>
      ) : (
        <Button variant="ghost" lift busy={binding} onClick={pickAudio}>
          <FolderOpen size={13} strokeWidth={2} /> {t('r.audio.pick')}
        </Button>
      )}

      {info.resolved && mismatch && (
        <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-warn">
          <AlertTriangle size={12} strokeWidth={2} className="mt-0.5 shrink-0" />
          {tf('r.audio.mismatch', {
            audio: fmtClock(player.durationMs),
            doc: fmtClock(info.duration_s * 1000),
          })}
        </p>
      )}
      {player.error && (
        <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-err">
          <AlertTriangle size={12} strokeWidth={2} className="mt-0.5 shrink-0" />
          {player.error}
        </p>
      )}
    </Card>
  );
}
