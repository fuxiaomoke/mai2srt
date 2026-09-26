# app

The desktop shell: a Tauri 2 window hosting the React UI, which talks to the Python
backend over `127.0.0.1:47613`.

- `src/routes/` — the three pages (transcribe, refine, settings) and the settings cards
- `src/components/` — shared UI: window chrome, buttons/cards/toasts, the audio card
- `src/lib/` — backend client, i18n dictionaries, theme, subtitle + player helpers
- `public/` — static assets: wallpapers, the UI font, the boot splash card
- `src-tauri/` — Rust side: window and splash lifecycle, sidecar staging
  (`src-tauri/backend/` is build output and is not tracked)
- `../src/mai2srt/` — the Python backend itself

Build and run instructions are in the [English README](../README.en.md) (the root
[README.md](../README.md) is the Chinese one, and GitHub shows that by default).

One trap worth knowing before you touch anything: the packaged backend under
`src-tauri/backend/` is a frozen PyInstaller build. `release.ps1 -SkipBackend` reuses it as-is,
so after changing anything under `../src/mai2srt/` you must run a full release instead.
