/** Appearance system: theme + material + glass/wallpaper tuning with FOUR
 *  independent parameter slots -- (light|dark) x (wallpaper|no-wallpaper).
 *
 *  The active slot follows the RESOLVED theme and whether a wallpaper is
 *  on; every slider writes into the active slot only, so each mode keeps
 *  its own tuned values and switching modes switches parameter sets.
 *  "Restore defaults" resets ONLY the active slot to its factory set
 *  (resetting all four means visiting each mode and clicking again --
 *  deliberate: the modes are independent).
 *
 *  The WALLPAPER CHOICE is per theme too (light and dark each hold their
 *  own source/path): switching theme must not drag the other theme's
 *  image along. */
import {
  createContext, useContext, useEffect, useState, type ReactNode,
} from 'react';
import { convertFileSrc } from '@tauri-apps/api/core';

export type ThemeName = 'rose' | 'ember';
export type ThemeMode = 'system' | 'rose' | 'ember';
export type Material = 'frosted' | 'liquid';
/** none = plain background; auto = bundled wallpaper following the theme;
 *  custom = a user-picked image file */
export type WallpaperSource = 'none' | 'auto' | 'custom';

/** one (light|dark) x (wall|nowall) combination */
export type SlotKey = 'light-wall' | 'light-nowall' | 'dark-wall' | 'dark-nowall';

export interface AppearanceParams {
  material: Material;
  glassOpacity: number;  // 0..1
  glassBlur: number;     // px, always concrete (slots never follow material)
  bgOpacity: number;     // 0..1
  wallOpacity: number;   // 0..1 (applies while a wallpaper is on)
  wallBlur: number;      // px
}

/** factory defaults, one set per slot (user-specified) */
export const FACTORY: Record<SlotKey, AppearanceParams> = {
  'light-wall': {
    material: 'liquid', glassOpacity: 0.45, glassBlur: 0,
    bgOpacity: 0.30, wallOpacity: 0.95, wallBlur: 0,
  },
  'light-nowall': {
    material: 'liquid', glassOpacity: 0.35, glassBlur: 0,
    bgOpacity: 0.95, wallOpacity: 0.95, wallBlur: 0,
  },
  'dark-wall': {
    material: 'liquid', glassOpacity: 0.35, glassBlur: 0,
    bgOpacity: 0.50, wallOpacity: 0.95, wallBlur: 0,
  },
  'dark-nowall': {
    material: 'liquid', glassOpacity: 0.30, glassBlur: 0,
    bgOpacity: 0.95, wallOpacity: 0.95, wallBlur: 0,
  },
};

const KEY = 'mai2srt.appearance';

/** one theme's wallpaper choice. Light and dark keep SEPARATE choices:
 *  picking a custom image in light mode must not bleed into dark (user
 *  report), so "custom here, default there" is a valid state. */
export interface Wallpaper {
  source: WallpaperSource;
  path: string;
}

const freshWallpaper = (): Wallpaper => ({ source: 'auto', path: '' });

/** validate one stored wallpaper blob; null when absent/unusable */
function readWallpaper(v: unknown): Wallpaper | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as { source?: unknown; path?: unknown };
  const src = o.source;
  return {
    source: src === 'none' || src === 'custom' ? src : 'auto',
    path: typeof o.path === 'string' ? o.path : '',
  };
}

interface Persisted {
  mode: ThemeMode;
  wallpapers: Record<ThemeName, Wallpaper>;
  slots: Record<SlotKey, AppearanceParams>;
}

function load(): Persisted {
  const fresh = (): Persisted => ({
    mode: 'system',
    wallpapers: { rose: freshWallpaper(), ember: freshWallpaper() },
    slots: {
      'light-wall': { ...FACTORY['light-wall'] },
      'light-nowall': { ...FACTORY['light-nowall'] },
      'dark-wall': { ...FACTORY['dark-wall'] },
      'dark-nowall': { ...FACTORY['dark-nowall'] },
    },
  });
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const p = JSON.parse(raw);
      const base = fresh();
      const slots = {} as Record<SlotKey, AppearanceParams>;
      (Object.keys(FACTORY) as SlotKey[]).forEach((k) => {
        slots[k] = { ...FACTORY[k], ...(p.slots?.[k] ?? {}) };
      });
      // migration: the pre-fix format kept ONE global wallpaper. Copy it
      // into BOTH themes so nothing changes visually on upgrade; the user
      // can then set either theme independently (that is the whole fix).
      const legacy = readWallpaper(p.wallpaper);
      return {
        mode: p.mode === 'rose' || p.mode === 'ember' ? p.mode : 'system',
        wallpapers: {
          rose: readWallpaper(p.wallpapers?.rose) ?? legacy ?? freshWallpaper(),
          ember: readWallpaper(p.wallpapers?.ember) ?? legacy ?? freshWallpaper(),
        },
        slots: p.slots ? slots : base.slots,
      };
    }
  } catch { /* corrupted storage: fall through to factory */ }
  return fresh();
}

/** Define the palette for the frames BEFORE React's apply() effect runs
 *  (undefined CSS variables otherwise). main.tsx resolves the rule below
 *  first, so no frame is ever painted in the other theme's palette. */
export function paintTheme(light: boolean): void {
  document.documentElement.dataset.theme = light ? 'rose' : 'ember';
}

/** "Follow the system" is a CLOCK rule: dark from NIGHT_START_HOUR until
 *  DAY_START_HOUR, light in between. It deliberately no longer reads the
 *  Windows theme flags. Windows 10 has no day/night theme switch of its own,
 *  and on a Custom preset the two flags are independent constants (dark
 *  taskbar, light apps), so a flag-based answer never moves -- it just picks
 *  a side and stays there, which is not what the user asked for. The clock
 *  is; and unlike a registry read it needs neither Rust nor the sidecar, so
 *  the very first painted frame is already correct.
 *  The mode keeps its name: `system` in storage, "跟随系统" in the UI. */
const DAY_START_HOUR = 7;
const NIGHT_START_HOUR = 19;

/** is `now` inside the day window, i.e. [07:00, 19:00) local time?
 *  Mirrored by the two surfaces that paint before React can -- the boot plug
 *  in index.html and the boot card in splash.html: keep all three in sync. */
export function systemLightAt(now: Date = new Date()): boolean {
  const h = now.getHours();
  return h >= DAY_START_HOUR && h < NIGHT_START_HOUR;
}

/** the saved appearance mode, readable before React mounts */
export function savedThemeMode(): ThemeMode | null {
  try {
    const m = JSON.parse(localStorage.getItem(KEY) || '{}').mode;
    return m === 'rose' || m === 'ember' || m === 'system' ? m : null;
  } catch {
    return null;
  }
}

function apply(theme: ThemeName, p: AppearanceParams) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.material = p.material;
  document.documentElement.style.setProperty('--glass-alpha', String(p.glassOpacity));
  // leaf surfaces scale multiplicatively so 0 = the whole UI can go fully
  // clear. ember inputs follow dsh-dream-skin: a white TINT hovering near
  // 8%, so a large multiplier would glare on the dark background.
  const inputMul = theme === 'ember' ? 0.32 : 1.6;
  document.documentElement.style.setProperty(
    '--glass-alpha-input', String(Math.min(1, p.glassOpacity * inputMul)));
  document.documentElement.style.setProperty(
    '--glass-alpha-nav', String(Math.min(1, p.glassOpacity * 1.4)));
  // base glow coat: a REAL opacity on the layer itself, composited
  // honestly over (or under) whatever wallpaper is active
  document.documentElement.style.setProperty('--glow-opacity', String(p.bgOpacity));
  document.documentElement.style.setProperty('--glass-blur', `${p.glassBlur}px`);
}

/** bundled wallpaper asset for the resolved theme ('/...' works in vite
 *  dev and in the built tauri webview alike). Both ship as 4K webp
 *  (generated from the authored png/jpg -- see _research/RESEARCH.md);
 *  keep names in sync with app/public/wallpapers/. */
export function bundledWallpaperUrl(light: boolean): string {
  return light ? '/wallpapers/background-light.webp'
               : '/wallpapers/background-dark.webp';
}

interface AppearanceApi {
  mode: ThemeMode;
  resolved: ThemeName;          // rose/ember after the follow-system (clock) rule
  slotKey: SlotKey;             // which of the four sets is live
  params: AppearanceParams;     // active slot's values (sliders bind here)
  /** the ACTIVE theme's wallpaper choice (each theme keeps its own) */
  wallpaper: Wallpaper;
  wallpaperUrl: string | null;  // resolved layer url (null = layer hidden)
  changeMode: (m: ThemeMode) => void;
  changeMaterial: (m: Material) => void;
  changeGlassOpacity: (v: number) => void;
  changeGlassBlur: (v: number) => void;
  changeBgOpacity: (v: number) => void;
  changeWallOpacity: (v: number) => void;
  changeWallBlur: (v: number) => void;
  useAutoWallpaper: () => void;
  useCustomWallpaper: (path: string) => void;
  clearWallpaper: () => void;
  /** reset the ACTIVE slot to its factory set (other slots untouched) */
  resetDefaults: () => void;
}

const Ctx = createContext<AppearanceApi | null>(null);

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Persisted>(load);
  // The follow-system rule is read DURING render: "now" is an external fact,
  // so re-deriving IS the refresh -- there is nothing to seed and nothing
  // that can go stale while a pinned mode is active. The tick below exists
  // only to force that render when a boundary passes under an open window
  // (07:00 / 19:00 are whole hours, so half a minute of lag is invisible),
  // and it runs in this mode alone.
  const [, clockTick] = useState(0);
  const sysLight = state.mode === 'system' ? systemLightAt() : false;

  useEffect(() => {
    if (state.mode !== 'system') return;
    const id = window.setInterval(() => clockTick((n) => n + 1), 30_000);
    return () => window.clearInterval(id);
  }, [state.mode]);

  useEffect(() => {
    localStorage.setItem(KEY, JSON.stringify(state));
  }, [state]);

  const resolved: ThemeName = state.mode === 'system'
    ? (sysLight ? 'rose' : 'ember')
    : state.mode;
  const light = resolved === 'rose';
  // the ACTIVE theme owns its wallpaper: editing it never touches the
  // other theme's choice (light custom + dark default is a valid state)
  const wallpaper = state.wallpapers[resolved];
  const hasWall = wallpaper.source !== 'none';
  const slotKey: SlotKey = `${light ? 'light' : 'dark'}-${hasWall ? 'wall' : 'nowall'}`;
  const params = state.slots[slotKey];

  useEffect(() => {
    apply(resolved, params);
  }, [resolved, params]);

  // slider writes land in the ACTIVE slot only -- that is the whole point
  // of the four-set design: each mode accumulates its own tuning
  const patchSlot = (patch: Partial<AppearanceParams>) =>
    setState((s) => ({
      ...s,
      slots: { ...s.slots, [slotKey]: { ...s.slots[slotKey], ...patch } },
    }));

  const setWallpaper = (w: Wallpaper) =>
    setState((s) => ({ ...s, wallpapers: { ...s.wallpapers, [resolved]: w } }));

  const wallpaperUrl = (() => {
    if (wallpaper.source === 'auto') return bundledWallpaperUrl(light);
    if (wallpaper.source === 'custom' && wallpaper.path) {
      try {
        return convertFileSrc(wallpaper.path);
      } catch {
        return null;
      }
    }
    return null;
  })();

  const api: AppearanceApi = {
    mode: state.mode,
    resolved,
    slotKey,
    params,
    wallpaper,
    wallpaperUrl,
    changeMode: (m) => setState((s) => ({ ...s, mode: m })),
    changeMaterial: (m) => patchSlot({ material: m }),
    changeGlassOpacity: (v) => patchSlot({ glassOpacity: v }),
    changeGlassBlur: (v) => patchSlot({ glassBlur: v }),
    changeBgOpacity: (v) => patchSlot({ bgOpacity: v }),
    changeWallOpacity: (v) => patchSlot({ wallOpacity: v }),
    changeWallBlur: (v) => patchSlot({ wallBlur: v }),
    useAutoWallpaper: () => setWallpaper({ source: 'auto', path: '' }),
    useCustomWallpaper: (path) => setWallpaper({ source: 'custom', path }),
    clearWallpaper: () => setWallpaper({ source: 'none', path: '' }),
    resetDefaults: () =>
      setState((s) => ({
        ...s,
        slots: { ...s.slots, [slotKey]: { ...FACTORY[slotKey] } },
      })),
  };
  return <Ctx.Provider value={api}>{children}</Ctx.Provider>;
}

export function useAppearance(): AppearanceApi {
  const api = useContext(Ctx);
  if (!api) throw new Error('useAppearance outside AppearanceProvider');
  return api;
}

/** Fixed wallpaper layer to mount once at the shell root (under the glow).
 *  opacity/blur come from the ACTIVE slot's wall params. */
export function WallpaperLayer({ url, opacity, blur }: {
  url: string | null; opacity: number; blur: number;
}) {
  if (!url) return null;
  /* blur edge-bleed fix: blur(N) degrades ~2N px at every edge (the kernel
     samples outside transparency). Instead of a fixed 3% scale jump at any
     blur > 0 (visible as a zoom step from 0 -> 1px), overscan the layer by
     2*blur so the ruined edge stays off-screen and the magnification grows
     CONTINUOUSLY with the blur amount (1px blur = 2px overscan, invisible). */
  const overscan = blur > 0 ? Math.ceil(blur * 2) : 0;
  return (
    <div
      aria-hidden
      style={{
        position: 'fixed',
        inset: overscan ? `-${overscan}px` : 0,
        zIndex: -2,
        borderRadius: 'var(--window-radius)',
        backgroundImage: `url(${url})`,
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        opacity,
        filter: blur ? `blur(${blur}px)` : undefined,
      }}
    />
  );
}
