/**
 * The About card: version, update state, release notes, and the two switches
 * that matter (check now / check automatically).
 *
 * Opened from the sidebar version button -- no new settings section, because
 * this is app identity, not configuration (agreed with the user 2026-11-16).
 * A portal + backdrop like the Select panel: `.glass` cards create stacking
 * contexts, so an in-page modal would be painted over by the next card.
 */
import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { motion } from 'framer-motion';
import { AudioWaveform, Download, Loader2, RefreshCw, X } from 'lucide-react';
import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Button, Toggle, useToast } from './ui';
import { useI18n } from '../lib/i18n';
import { openUrl } from '../lib/api';
import { refreshUpdate, setAutomaticChecks, useUpdate } from '../lib/update';

/** Release bodies ARE markdown (see docs/release-notes/*.md -- headings,
 *  bullets, bold, inline code, a horizontal rule, and one bare URL), so they
 *  are rendered rather than text-dumped. Earlier this stripped `#` and `**`
 *  and showed the rest pre-wrapped; that is not a rendering, it just removes
 *  the two ugliest markers (user feedback, 2026-11-16).
 *
 *  Every element is mapped to the card's own type scale instead of pulling in
 *  a typography plugin: this is one 11.5 px panel inside a 540 px modal, and
 *  prose defaults would be twice the size of everything around them.
 *
 *  Links are the one functional override: an `<a href>` inside a webview would
 *  NAVIGATE THE APP away from the UI. They open in the system browser instead,
 *  through the backend, like the download button. */
const MD_COMPONENTS: Components = {
  h1: ({ children }) => (
    <p className="mb-1.5 mt-3 text-[12.5px] font-semibold text-ink-1 first:mt-0">{children}</p>
  ),
  h2: ({ children }) => (
    <p className="mb-1 mt-3 text-[12px] font-semibold text-ink-1 first:mt-0">{children}</p>
  ),
  h3: ({ children }) => (
    <p className="mb-1 mt-2.5 text-[11.5px] font-semibold text-ink-1 first:mt-0">{children}</p>
  ),
  p: ({ children }) => <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => (
    <ul className="my-1.5 list-disc space-y-1 pl-4 first:mt-0 last:mb-0">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="my-1.5 list-decimal space-y-1 pl-4 first:mt-0 last:mb-0">{children}</ol>
  ),
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-ink-1">{children}</strong>,
  code: ({ children }) => (
    <code className="rounded bg-ink-1/10 px-1 py-px font-mono text-[10.5px] text-ink-1">
      {children}
    </code>
  ),
  a: ({ href, children }) => <ExternalLink href={href}>{children}</ExternalLink>,
  hr: () => <hr className="my-3 border-t border-line-1" />,
};

function ExternalLink({ href, children }: {
  href?: string;
  children: React.ReactNode;
}) {
  const toast = useToast();
  return (
    <a
      href={href}
      onClick={(e) => {
        // never let the webview navigate itself away from the app
        e.preventDefault();
        if (!href) return;
        openUrl(href).catch((err) => toast('error', String(err).slice(0, 160)));
      }}
      className="text-primary underline decoration-primary/40 underline-offset-2 transition-colors hover:decoration-primary"
    >
      {children}
    </a>
  );
}

export function AboutCard({ version, onClose }: {
  version: string;
  onClose: () => void;
}) {
  const { t, tf } = useI18n();
  const toast = useToast();
  const up = useUpdate((s) => s);

  // opening the card is a good moment to have a fresh-ish answer: the backend
  // rate-limits this to one request a day, so asking costs nothing when it is
  // already known
  useEffect(() => { void refreshUpdate(false); }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  async function download() {
    if (!up.latest?.url) return;
    try {
      await openUrl(up.latest.url);
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    }
  }

  const shown = version || up.current;
  const checked = up.checkedAt ? up.checkedAt.slice(11, 16) : '';

  return createPortal(
    <>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.16 }}
        className="fixed inset-0 z-[60] bg-black/35"
        onClick={onClose}
      />
      <div className="pointer-events-none fixed inset-0 z-[61] grid place-items-center p-6">
        {/* y+scale only: an opacity animation on a glass surface isolates the
            compositor layer and breaks backdrop-filter sampling (the glass
            flash bug) */}
        <motion.div
          initial={{ y: 10, scale: 0.98 }}
          animate={{ y: 0, scale: 1 }}
          transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
          className="glass-modal pointer-events-auto flex w-full max-w-[540px] flex-col rounded-(--radius-l) p-5"
        >
          <div className="flex items-start gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-(--radius-s) bg-primary-dim text-primary">
              <AudioWaveform size={16} strokeWidth={2} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[14px] font-semibold text-ink-1">{t('about.title')}</p>
              <p className="mt-0.5 truncate font-mono text-[11px] text-ink-3">
                {t('about.current')} v{shown || '?'}
                {checked && <span className="ml-2">{tf('about.checkedAt', { t: checked })}</span>}
              </p>
            </div>
            <button
              onClick={onClose}
              title={t('about.close')}
              className="grid h-8 w-8 shrink-0 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-ink-1"
            >
              <X size={14} strokeWidth={2} />
            </button>
          </div>

          <div className="mt-4 min-h-[52px]">
            {up.checking ? (
              <p className="flex items-center gap-2 text-[12.5px] text-ink-2">
                <Loader2 size={13} strokeWidth={2} className="animate-spin" />
                {t('about.checking')}
              </p>
            ) : up.latest ? (
              <>
                <p className="text-[13px] font-semibold text-primary">
                  {tf('about.newVersion', { v: up.latest.version })}
                </p>
                {up.latest.published_at && (
                  <p className="mt-0.5 font-mono text-[10.5px] text-ink-3">
                    {up.latest.published_at.slice(0, 10)}
                  </p>
                )}
                <p className="mt-3 text-[10.5px] font-medium uppercase tracking-wide text-ink-3">
                  {t('about.notes')}
                </p>
                <div className="mt-1.5 max-h-72 overflow-y-auto rounded-(--radius-s) border border-line-1 bg-ink-1/5 p-3 text-[11.5px] leading-relaxed text-ink-2">
                  {up.latest.notes.trim() ? (
                    <Markdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                      {up.latest.notes}
                    </Markdown>
                  ) : (
                    <p>{t('about.noNotes')}</p>
                  )}
                </div>
              </>
            ) : up.error ? (
              <p className="text-[12px] text-warn">
                {tf('about.failed', { err: up.error.slice(0, 160) })}
              </p>
            ) : up.loaded ? (
              <p className="flex items-center gap-2 text-[12.5px] text-ok">
                <AudioWaveform size={13} strokeWidth={2} />
                {t('about.upToDate')}
              </p>
            ) : (
              <p className="text-[12.5px] text-ink-3">
                {up.auto ? t('about.checking') : t('about.autoOff')}
              </p>
            )}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-line-1 pt-3">
            {up.latest?.url && (
              <Button onClick={download}>
                <Download size={13} strokeWidth={2} /> {t('about.download')}
              </Button>
            )}
            <Button
              variant="ghost"
              busy={up.checking}
              onClick={() => void refreshUpdate(true)}
            >
              <RefreshCw size={13} strokeWidth={2} /> {t('about.check')}
            </Button>
            <div className="ml-auto">
              <Toggle
                checked={up.auto}
                onChange={(v) => void setAutomaticChecks(v)}
                label={t('about.auto')}
              />
            </div>
          </div>
        </motion.div>
      </div>
    </>,
    document.body,
  );
}
