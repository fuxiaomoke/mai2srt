# Third-party notices

mai2srt itself is free software under the **GNU General Public License v3** — see [LICENSE](./LICENSE).
Everything listed here is **someone else's work**, redistributed inside the installer under its own
license. mai2srt's license does not change those terms, and these components do not become GPL
because they ship alongside mai2srt.

Where a license has to travel with the binaries, the installed copy sits next to the component:

| Installed path | What it is |
| --- | --- |
| `ffmpeg\LICENSE.txt` | GPLv3 text covering the bundled FFmpeg binaries |
| `ffmpeg\README.txt` | FFmpeg build information (version, configure line, libraries) |
| `ffmpeg\SOURCE.txt` | where to get the exact source of those binaries |
| `licenses\LICENSE` | mai2srt's own GPLv3 text |
| `licenses\THIRD-PARTY-NOTICES.md` | this file |
| `licenses\LXGW-Yozai-OFL-1.1.txt` | SIL OFL 1.1 text covering the bundled UI font |
| `_internal\playwright\driver\LICENSE`, `...\driver\package\NOTICE` | Playwright / Node.js notices |

## Bundled binaries

### FFmpeg 9.0.2 — GNU General Public License v3

- **Files**: `ffmpeg\ffmpeg.exe`, `ffmpeg\ffprobe.exe` (static builds, ~100 MB each)
- **Build**: `9.0.2-essentials_build` from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), configured with
  `--enable-gpl --enable-version3` — this is why the binaries are GPLv3 rather than LGPL.
- **Copyright**: the FFmpeg developers. FFmpeg is a trademark of Fabrice Bellard.
- **Source** (unmodified upstream): <https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz>, corresponding to
  upstream commit <https://github.com/FFmpeg/FFmpeg/commit/946fcce07b>. The exact configure line of the
  shipped build is recorded in `ffmpeg\README.txt`.
- **How mai2srt uses it**: invoked as a **separate process** over its command line. mai2srt does not
  link against FFmpeg libraries, so FFmpeg's license does not extend to mai2srt's own code.

### LXGW Yozai / 悠哉字体 Medium v0.861 — SIL Open Font License 1.1

- **File**: the application UI font, shipped as `CustomUI-Regular.ttf` (unmodified)
- **Copyright**: 2020, 2024 LXGW (<https://github.com/lxgw/yozai-font>); original font data
  Copyright (C) Y.Oz (Y.OzVox) (<http://yozvox.web.fc2.com>)
- **Reserved font name**: names containing `Y.Oz` or `YOz`. The font is redistributed unmodified, so
  this does not restrict anything here — but if you ever subset or modify it, do not use one of those
  names for the result, and keep the font under the OFL.
- **Full text**: [`licenses/LXGW-Yozai-OFL-1.1.txt`](./licenses/LXGW-Yozai-OFL-1.1.txt). The font also
  carries the same copyright and license notice inside its own metadata.

### Playwright 1.56.0 — Apache License 2.0

- **Files**: the `playwright` Python package plus the bundled Node driver in `_internal\playwright\driver\`
- **Copyright**: Microsoft Corporation — <https://github.com/microsoft/playwright>
- Drives the user's own installed Chrome/Edge for playground sign-in. **No Chromium browser binary is
  redistributed**; only the driver (including the Node runtime below) ships with mai2srt.

### Node.js v22.20.0 — MIT License

- **File**: `_internal\playwright\driver\node.exe`, the runtime the Playwright driver executes on
- **Copyright**: the Node.js contributors — <https://github.com/nodejs/node>
- Node's own bundled third-party notices travel with it in
  `_internal\playwright\driver\package\NOTICE`.

### Python 3.13 runtime — Python Software Foundation License 2.0

- **File**: `python313.dll` and the standard library inside the frozen backend
- <https://docs.python.org/3/license.html>

### PyInstaller 6.14.1 bootloader — GPL-2.0-or-later with a special exception

- **File**: the bootloader embedded in `mai2srt-backend.exe`
- <https://github.com/pyinstaller/pyinstaller>
- PyInstaller's exception explicitly permits packaging and distributing frozen applications under
  **any** license, so it imposes nothing on mai2srt's own terms. PyInstaller is otherwise a build
  tool; only the bootloader is redistributed.

## Python packages frozen into the backend

These are built into `mai2srt-backend.exe` / `_internal\`. Each keeps its own license; the source for
every one of them is on PyPI under the same name.

| Package | Version | License |
| --- | --- | --- |
| annotated-types | 0.7.0 | MIT |
| anyio | 4.12.0 | MIT |
| click | 8.3.1 | BSD-3-Clause |
| colorama | 0.4.6 | BSD-3-Clause |
| fastapi | 0.115.5 | MIT |
| greenlet | 3.2.4 | MIT |
| h11 | 0.16.0 | MIT |
| httptools | 0.8.0 | MIT |
| idna | 3.10 | BSD-3-Clause |
| importlib-metadata | 8.0.0 | Apache-2.0 (vendored inside setuptools) |
| numpy | 2.3.3 | BSD-3-Clause |
| OpenBLAS (`numpy.libs`) | — | BSD-3-Clause |
| pydantic | 2.10.3 | MIT |
| pydantic-core | 2.27.1 | MIT |
| pyee | 13.0.0 | MIT |
| python-dotenv | 1.0.1 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pytz | 2025.2 | MIT |
| PyYAML | 6.0.3 | MIT |
| rich | 15.0.0 | MIT |
| setuptools | 80.9.0 | MIT |
| sniffio | 1.3.1 | MIT or Apache-2.0 |
| starlette | 0.41.3 | BSD-3-Clause |
| typing-extensions | 4.15.0 | PSF-2.0 |
| uvicorn | 0.32.1 | BSD-3-Clause |
| watchfiles | 1.2.0 | MIT |
| websockets | 16.0 | BSD-3-Clause |

## Frontend libraries compiled into the app bundle

| Library | License |
| --- | --- |
| React, React DOM | MIT |
| framer-motion (motion-dom, motion-utils) | MIT |
| lucide-react (icons) | ISC |
| Tailwind CSS (runtime utility output) | MIT |
| Inter, JetBrains Mono (via `@fontsource-variable/*`) | SIL OFL 1.1 |
| Tauri JavaScript API (`@tauri-apps/api`, plugins) | MIT or Apache-2.0 |

## Rust crates linked into the application

`tauri`, `tauri-build`, `tauri-plugin-dialog`, `tauri-plugin-opener`, `tauri-plugin-shell`, `serde`,
`serde_json`, `winreg`, `windows-sys` and their transitive dependencies — predominantly MIT or
Apache-2.0. The exact set and versions are pinned in [`app/src-tauri/Cargo.lock`](./app/src-tauri/Cargo.lock),
which is the authoritative list; each crate carries its license text in its own repository.

## Not redistributed

Build-time-only tools — Vite, TypeScript, Tailwind CLI, oxlint, the Tauri CLI, pip, and PyInstaller
when used as a tool — run on the maintainer's machine. Only what they generate ships, so their own
licenses do not attach to the installer.

## Corresponding source for mai2srt

The Corresponding Source for a released installer is this project's source tree **at the tag matching
that release** (for example `v0.1.0`), including the build scripts under `scripts/` and `packaging/`
that produce it. The clone URL is in [README.md](./README.md) (Chinese; the English copy is
[README.en.md](./README.en.md)).
