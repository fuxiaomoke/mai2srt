import {
  FolderOpen, Image as ImageIcon, Languages, Layers, RotateCcw, Sun, Trash2, Wand2,
} from 'lucide-react';
import { open } from '@tauri-apps/plugin-dialog';
import { Button, Card, Select, useToast } from '../../components/ui';
import { useI18n, type Lang } from '../../lib/i18n';
import {
  useAppearance, type Material, type ThemeMode,
} from '../../lib/appearance';
import { SettingRow, Slider } from './controls';

/* ------------------------------------------------------ settings > appearance */

export function AppearanceSettings() {
  const { t, lang, setLang } = useI18n();
  // context-backed appearance: sliders bind the ACTIVE slot's params and
  // every change writes into that slot only (four independent sets)
  const {
    mode, params, wallpaper, slotKey, resolved,
    changeMode, changeMaterial, changeGlassOpacity, changeGlassBlur,
    changeBgOpacity, changeWallOpacity, changeWallBlur,
    useAutoWallpaper, useCustomWallpaper, clearWallpaper, resetDefaults,
  } = useAppearance();
  const toast = useToast();

  async function pickWallpaper() {
    const p = await open({
      multiple: false,
      filters: [{ name: 'image', extensions: ['png', 'jpg', 'jpeg', 'webp', 'avif', 'bmp'] }],
    });
    if (typeof p === 'string') useCustomWallpaper(p);
  }

  return (
    /* min-h-full + justify-center: a card that cannot fill the page
     * floats mid-screen instead of crowding the top (the transcribe
     * page's empty-state pattern); when it outgrows the viewport the
     * wrapper grows and scrolls as before */
    <div className="mx-auto flex min-h-full w-full max-w-[min(1280px,94%)] flex-col justify-center gap-5 px-8 py-8">
      <Card className="flex flex-col gap-5 p-6">
        <div className="flex items-center justify-between">
          <span className="text-[13px] font-semibold text-ink-1">{t('s.appearance')}</span>
          {/* resets ONLY the active slot's set: modes are independent, so
              resetting all four means visiting each mode and clicking */}
          <Button
            variant="ghost"
            lift
            onClick={() => { resetDefaults(); toast('ok', t('s.toast.appearanceReset')); }}
            title={t('s.appearance.resetHint')}
          >
            <RotateCcw size={13} strokeWidth={2} /> {t('s.appearance.reset')}
          </Button>
        </div>

        {/* theme / material / language */}
        <div className="grid grid-cols-3 gap-x-8 gap-y-3">
          <SettingRow center icon={<Sun size={14} strokeWidth={2} className="text-ink-3" />} label={t('s.theme')}>
            <Select
              className="w-9/10"
              value={mode}
              onChange={(v) => changeMode(v as ThemeMode)}
              options={[
                { value: 'system', label: t('s.theme.system') },
                { value: 'rose', label: t('s.theme.rose') },
                { value: 'ember', label: t('s.theme.ember') },
              ]}
            />
          </SettingRow>
          <SettingRow center icon={<Wand2 size={14} strokeWidth={2} className="text-ink-3" />} label={t('s.material')}>
            <Select
              className="w-9/10"
              value={params.material}
              onChange={(v) => changeMaterial(v as Material)}
              options={[
                { value: 'liquid', label: t('s.material.liquid') },
                { value: 'frosted', label: t('s.material.frosted') },
            ]}
            />
          </SettingRow>
          <SettingRow center icon={<Languages size={14} strokeWidth={2} className="text-ink-3" />} label={t('s.language')}>
            <Select
              className="w-9/10"
              value={lang}
              onChange={(v) => setLang(v as Lang)}
              options={[
                { value: 'zh', label: '中文' },
                { value: 'en', label: 'English' },
              ]}
            />
          </SettingRow>
        </div>

        {/* glass tuning: fill opacity + backdrop blur, both user-owned.
            The slot badge reminds WHICH of the four sets is live. */}
        <div className="border-t border-line-1 pt-4">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-2 text-[12px] font-semibold text-ink-2">
              <Layers size={13} strokeWidth={2} className="text-ink-3" />
              {t('s.glass')}
            </span>
            <span className="font-mono text-[10.5px] text-ink-3">{slotKey}</span>
          </div>
          {/* one slider per row (user layout): each row spans the card,
              the track floats centered in the leftover space. Shared
              fixed label column -> identical leftover width -> the five
              tracks are exactly the same length */}
          <div className="mt-3 flex flex-col gap-3">
            <SettingRow center labelClass="w-[7rem]" label={t('s.glassOpacity')}>
              <Slider
                widthClass="w-9/10 flex-none"
                value={params.glassOpacity} min={0} max={1} step={0.05}
                onChange={changeGlassOpacity}
                format={(v) => `${Math.round(v * 100)}%`}
              />
            </SettingRow>
            <SettingRow center labelClass="w-[7rem]" label={t('s.glassBlur')}>
              <Slider
                widthClass="w-9/10 flex-none"
                value={params.glassBlur} min={0} max={40} step={1}
                onChange={changeGlassBlur}
                format={(v) => `${Math.round(v)}px`}
              />
            </SettingRow>
          </div>
        </div>

        {/* background: glow base coat (always) + optional wallpaper layer.
            the two opacity sliders are INDEPENDENT layer opacities, not one
            composited fudge — base coat dims the glow, wallpaper opacity
            dims the image on top of whatever is beneath it */}
        <div className="border-t border-line-1 pt-4">
          <div className="flex items-center justify-between">
            <span className="flex items-center gap-2 text-[12px] font-semibold text-ink-2">
              <ImageIcon size={13} strokeWidth={2} className="text-ink-3" />
              {t('s.bg')}
            </span>
            {/* the wallpaper belongs to ONE theme -- name it, so switching
                the theme and finding another image is never a surprise */}
            <span className="font-mono text-[10.5px] text-ink-3">
              {resolved === 'rose' ? t('s.theme.rose') : t('s.theme.ember')}
            </span>
          </div>
          <div className="mt-3 flex items-center gap-3">
            <Button
              variant="ghost"
              lift
              onClick={useAutoWallpaper}
              title={t('s.wallpaper.autoHint')}
            >
              <ImageIcon size={13} strokeWidth={2} /> {t('s.wallpaper.auto')}
            </Button>
            <Button variant="ghost" lift onClick={pickWallpaper}>
              <FolderOpen size={13} strokeWidth={2} /> {t('s.wallpaper.pick')}
            </Button>
            {wallpaper.source !== 'none' && (
              <>
                <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-ink-3">
                  {wallpaper.source === 'auto' ? t('s.wallpaper.autoHint') : wallpaper.path}
                </span>
                <Button variant="ghost" lift onClick={clearWallpaper}>
                  <Trash2 size={13} strokeWidth={2} /> {t('s.wallpaper.remove')}
                </Button>
              </>
            )}
          </div>
          {/* one slider per row (user layout), same fixed label column
              as the glass block */}
          <div className="mt-3 flex flex-col gap-3">
            <SettingRow center labelClass="w-[7rem]" label={t('s.bg.opacity')}>
              <Slider
                widthClass="w-9/10 flex-none"
                value={params.bgOpacity} min={0} max={1} step={0.05}
                onChange={changeBgOpacity}
                format={(v) => `${Math.round(v * 100)}%`}
              />
            </SettingRow>
            {wallpaper.source !== 'none' && (
              <>
                <SettingRow center labelClass="w-[7rem]" label={t('s.wallpaper.opacity')}>
                  <Slider
                    widthClass="w-9/10 flex-none"
                    value={params.wallOpacity} min={0} max={1} step={0.05}
                    onChange={changeWallOpacity}
                    format={(v) => `${Math.round(v * 100)}%`}
                  />
                </SettingRow>
                <SettingRow center labelClass="w-[7rem]" label={t('s.wallpaper.blur')}>
                  <Slider
                    widthClass="w-9/10 flex-none"
                    value={params.wallBlur} min={0} max={40} step={1}
                    onChange={changeWallBlur}
                    format={(v) => `${Math.round(v)}px`}
                  />
                </SettingRow>
              </>
            )}
          </div>
        </div>
      </Card>
    </div>
  );
}
