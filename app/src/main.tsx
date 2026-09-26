import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { invoke } from '@tauri-apps/api/core'
import './index.css'
import App from './App.tsx'
import { primeWarm } from './lib/api'
import { paintTheme, savedThemeMode, systemLightAt } from './lib/appearance'

/** resolves on the frame AFTER the next one is on screen */
const nextPaint = () => new Promise<void>((done) => {
  requestAnimationFrame(() => requestAnimationFrame(() => done()))
})

const delay = (ms: number) => new Promise<void>((done) => setTimeout(done, ms))

/** Boot order -- every step exists for a reason:
 *
 *  1. Resolve the theme BEFORE the first render, by the same rule React
 *     will apply: a PINNED mode (rose/ember) wins outright, while under
 *     "follow system" the clock decides -- dark from 19:00 until 07:00
 *     (systemLightAt in appearance.tsx). Both answers are available
 *     synchronously, so the first frame is already right and nothing waits
 *     on Rust or the sidecar: a cold start cannot flash the other theme.
 *     The boot card in splash.html resolves its palette by the same rule.
 *  2. Render, then wait for that frame to be on screen.
 *  3. Wait for the startup prefetch: pages seed their first paint from it,
 *     so this is what "everything the window needs is loaded" means. It is
 *     bounded -- a wedged backend must not hold the splash hostage.
 *  4. Hand over to Rust: it closes the splash card and shows the real
 *     window, whose taskbar button is born at that moment (the window is
 *     created hidden). Rust keeps a watchdog in case this never runs.
 */
async function boot() {
  const mode = savedThemeMode() ?? 'system'
  paintTheme(mode === 'system' ? systemLightAt() : mode === 'rose')

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )

  await Promise.race([nextPaint(), delay(250)])
  await Promise.race([primeWarm(), delay(15000)])
  // This install has completed a boot -- the marker the boot card reads to
  // tell a genuine first launch (sidecar behind an AV scan) from a slow one.
  try { localStorage.setItem('mai2srt.booted', '1') } catch { /* blocked storage */ }
  await invoke('app_ready').catch(() => {})
}

boot()
