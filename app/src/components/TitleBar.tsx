import { Minus, Square, X, AudioWaveform } from 'lucide-react';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { useCallback } from 'react';
import { useI18n } from '../lib/i18n';

const win = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
  ? getCurrentWindow()
  : null;

/** Frameless titlebar: drag region + lucide window controls. */
export function TitleBar() {
  const { t } = useI18n();
  const minimize = useCallback(() => win?.minimize(), []);
  const toggleMax = useCallback(() => win?.toggleMaximize(), []);
  const close = useCallback(() => win?.close(), []);

  return (
    <header
      data-tauri-drag-region
      className="glass-nav relative z-20 flex h-11 shrink-0 select-none items-center justify-between border-b border-line-1"
    >
      <div data-tauri-drag-region className="flex items-center gap-2.5 pl-4">
        <AudioWaveform size={16} strokeWidth={2} className="text-primary" />
        <span
          data-tauri-drag-region
          className="text-[13px] font-semibold tracking-wide text-ink-1"
        >
          mai2srt
        </span>
      </div>
      <div className="flex h-full">
        <button
          onClick={minimize}
          className="flex h-full w-12 items-center justify-center text-ink-2 transition-colors duration-150 hover:bg-ink-1/15 active:bg-ink-1/25"
          title={t('win.min')}
        >
          <Minus size={15} strokeWidth={2} />
        </button>
        <button
          onClick={toggleMax}
          className="flex h-full w-12 items-center justify-center text-ink-2 transition-colors duration-150 hover:bg-ink-1/15 active:bg-ink-1/25"
          title={t('win.max')}
        >
          <Square size={12} strokeWidth={2} />
        </button>
        <button
          onClick={close}
          className="flex h-full w-12 items-center justify-center text-ink-2 transition-colors duration-150 hover:bg-err hover:text-white"
          title={t('win.close')}
        >
          <X size={15} strokeWidth={2} />
        </button>
      </div>
    </header>
  );
}
