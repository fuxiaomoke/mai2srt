import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  AudioLines, Brain, ChevronDown, FolderOpen, Palette, Settings2, SlidersHorizontal,
} from 'lucide-react';
import { TitleBar } from './components/TitleBar';
import { ToastHost } from './components/ui';
import { primeWarm, getSystem, warmSystem, withBackendRetry } from './lib/api';
import { AppearanceProvider, WallpaperLayer, useAppearance } from './lib/appearance';
import { I18nProvider, useI18n, type Key } from './lib/i18n';
import { TranscribePage } from './routes/Transcribe';
import { RefinePage } from './routes/Refine';
import { AppearanceSettings } from './routes/settings/Appearance';
import { FilesSettings } from './routes/settings/Files';
import { LlmSettings } from './routes/settings/Llm';

type SettingsSection = 'settings/appearance' | 'settings/files' | 'settings/llm';
type Page = 'transcribe' | 'refine' | SettingsSection;

const SETTINGS_SECTIONS: { id: SettingsSection; label: Key; icon: typeof Palette }[] = [
  { id: 'settings/appearance', label: 'nav.appearance', icon: Palette },
  { id: 'settings/files', label: 'nav.files', icon: FolderOpen },
  { id: 'settings/llm', label: 'nav.llm', icon: Brain },
];

const isSettings = (p: Page): p is SettingsSection => p.startsWith('settings/');

function Shell() {
  const [page, setPage] = useState<Page>('transcribe');
  /** settings submenu fold state; entering a settings page always opens
   * it, leaving for transcribe/refine folds it. Clicking the group while
   * on a subpage folds the menu WITHOUT navigating away. */
  const [settingsOpen, setSettingsOpen] = useState(false);
  // "click the group -> land where I left off" (first visit: appearance)
  const lastSection = useRef<SettingsSection>('settings/appearance');
  // mount the appearance effects once at the shell root (provider applies
  // theme CSS vars + exposes the wallpaper layer data)
  const appearance = useAppearance();
  const { t } = useI18n();

  const go = (p: Page) => {
    if (isSettings(p)) lastSection.current = p;
    setSettingsOpen(isSettings(p));
    setPage(p);
  };

  // warm the settings payloads once per launch: pages then seed their
  // state from the cache and paint COMPLETE on their first frame (no
  // components materializing inside the slide-in)
  useEffect(() => { primeWarm(); }, []);

  // The shell runs inside a webview, whose own shortcuts mean nothing here and
  // surface as browser UI on top of the app: Ctrl+S opens the webview's "save
  // page" dialog, Ctrl+P a print dialog, Ctrl+O a file picker. Swallow that
  // class of key app-wide, so no page has to remember to -- and pages still
  // receive the event to give it their own meaning, because preventDefault
  // does not stop propagation (Refine turns Ctrl+S into "store the edit
  // record" while it has one to store, and is silent otherwise).
  useEffect(() => {
    const swallow = (ev: KeyboardEvent) => {
      const mod = ev.ctrlKey || ev.metaKey;
      const k = ev.key.toLowerCase();
      // browser dialogs with no counterpart in a desktop app
      if (mod && !ev.altKey && (k === 's' || k === 'p' || k === 'o')) {
        ev.preventDefault();
        return;
      }
      // reload keys: a legitimate way to restart the UI under `tauri dev`,
      // destructive in a packaged build (the loaded project and any pending
      // edit go with it)
      if (!import.meta.env.DEV && (k === 'f5' || (mod && !ev.altKey && k === 'r'))) {
        ev.preventDefault();
      }
    };
    window.addEventListener('keydown', swallow);
    return () => window.removeEventListener('keydown', swallow);
  }, []);

  // footer version: app identity, not the upstream engine (the sidebar
  // is the app's face). Seeded from the warm cache; retried fetch when
  // the cache is cold (packaged first launch: the sidecar takes seconds)
  const [appVer, setAppVer] = useState(() => warmSystem()?.version ?? '');
  useEffect(() => {
    if (appVer) return;
    withBackendRetry(getSystem, 12, 800)
      .then((s) => setAppVer(s.version))
      .catch(() => {});
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // cross-page navigation (e.g. the storage card opening a project in
  // refine, or the transcribe page's "去精修" hand-off)
  useEffect(() => {
    const onNav = (e: Event) => {
      const p = (e as CustomEvent<string>).detail;
      if (p === 'transcribe' || p === 'refine'
          || SETTINGS_SECTIONS.some((s) => s.id === p)) go(p as Page);
    };
    window.addEventListener('mai2srt:nav', onNav);
    return () => window.removeEventListener('mai2srt:nav', onNav);
  }, []);

  const clickSettings = () => {
    if (isSettings(page)) setSettingsOpen((o) => !o);   // fold/unfold, stay
    else go(lastSection.current);                       // open + enter
  };

  return (
    <div className="flex h-full flex-col">
      <WallpaperLayer
        url={appearance.wallpaperUrl}
        opacity={appearance.params.wallOpacity}
        blur={appearance.params.wallBlur}
      />
      <TitleBar />
      <div className="flex min-h-0 flex-1">
        {/* side nav */}
        <nav className="glass-nav flex w-[188px] shrink-0 flex-col gap-1 border-r border-line-1 p-3">
          {([['transcribe', 'nav.transcribe', AudioLines],
             ['refine', 'nav.refine', SlidersHorizontal]] as const).map(
            ([id, label, Icon]) => {
              const active = page === id;
              return (
                <button
                  key={id}
                  onClick={() => go(id)}
                  className={`relative flex items-center gap-3 rounded-(--radius-s) px-3 py-2.5 text-[13px] font-medium transition-colors duration-(--dur-in) ${
                    active ? 'text-primary' : 'text-ink-2 hover:bg-hover hover:text-ink-1'
                  }`}
                >
                  {active && (
                    <motion.span
                      layoutId="nav-pill"
                      className="absolute inset-0 rounded-(--radius-s) bg-primary-dim"
                      transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
                    />
                  )}
                  <Icon size={16} strokeWidth={2} className="relative z-10" />
                  <span className="relative z-10">{t(label)}</span>
                </button>
              );
            },
          )}

          {/* settings group: click to expand (and enter the last section),
              click again to fold -- the three sub-pages stay reachable.
              The chevron ONLY surfaces on hover (rotated while the
              submenu is open); at rest it is invisible in every state. */}
          <button
            onClick={clickSettings}
            className={`group relative flex items-center gap-3 rounded-(--radius-s) px-3 py-2.5 text-[13px] font-medium transition-colors duration-(--dur-in) ${
              isSettings(page) ? 'text-primary' : 'text-ink-2 hover:bg-hover hover:text-ink-1'
            }`}
          >
            {isSettings(page) && (
              <motion.span
                layoutId="nav-pill"
                className="absolute inset-0 rounded-(--radius-s) bg-primary-dim"
                transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
              />
            )}
            <Settings2 size={16} strokeWidth={2} className="relative z-10" />
            <span className="relative z-10">{t('nav.settings')}</span>
            <ChevronDown
              size={14} strokeWidth={2}
              className={`relative z-10 ml-auto opacity-0 transition-[opacity,rotate] duration-(--dur-in) group-hover:opacity-100 ${
                settingsOpen ? 'rotate-180' : ''
              }`}
            />
          </button>

          <AnimatePresence initial={false}>
            {settingsOpen && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
                className="overflow-hidden"
              >
                <div className="flex flex-col gap-1 pb-1 pl-4 pt-1">
                  {SETTINGS_SECTIONS.map(({ id, label, icon: Icon }) => {
                    const active = page === id;
                    return (
                      <button
                        key={id}
                        onClick={() => go(id)}
                        className={`relative flex items-center gap-2.5 rounded-(--radius-s) px-3 py-2 text-[12.5px] transition-colors duration-(--dur-in) ${
                          active ? 'text-primary' : 'text-ink-3 hover:bg-hover hover:text-ink-1'
                        }`}
                      >
                        {active && (
                          <motion.span
                            layoutId="nav-sub-pill"
                            className="absolute inset-0 rounded-(--radius-s) bg-primary-dim"
                            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
                          />
                        )}
                        <Icon size={14} strokeWidth={2} className="relative z-10" />
                        <span className="relative z-10 truncate">{t(label)}</span>
                      </button>
                    );
                  })}
                </div>
              </motion.div>
            )}
          </AnimatePresence>

          <div className="mt-auto px-3 pb-1 text-[11px] text-ink-3">
            {appVer ? `mai2srt v${appVer}` : 'mai2srt'}
          </div>
        </nav>

        {/* page body: the animated motion.div IS the scroller -- a
            transformed child of an overflow-y-auto parent momentarily
            extends its scrollable overflow (CSS Overflow 3: "scrollbars
            can sometimes appear when not actually necessary"), so the
            old main-level scrollbar flashed on every page switch and
            its layout-space width made the content jitter. main now
            just clips; each page scrolls inside its own container. */}
        <main className="min-w-0 flex-1 overflow-clip">
          <AnimatePresence mode="wait">
            <motion.div
              key={page}
              /* ENTER-ONLY transition: the exiting page must NOT move.
                An 8px exit nudge reads as a jitter, not a slide-out
                (the whole page shifts, then vanishes). The incoming
                page slides up from y:12 instead -- all motion belongs
                to the new content. y-only: opacity animation isolates
                the compositor layer and breaks backdrop-filter
                sampling during the transition (the glass flash bug). */
              initial={{ y: 12 }}
              animate={{ y: 0 }}
              transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
              className="h-full overflow-y-auto"
            >
              {page === 'transcribe' && <TranscribePage />}
              {page === 'refine' && <RefinePage />}
              {page === 'settings/appearance' && <AppearanceSettings />}
              {page === 'settings/files' && <FilesSettings />}
              {page === 'settings/llm' && <LlmSettings />}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <AppearanceProvider>
        <ToastHost>
          <Shell />
        </ToastHost>
      </AppearanceProvider>
    </I18nProvider>
  );
}
