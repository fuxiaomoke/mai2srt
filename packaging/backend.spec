# PyInstaller spec for the mai2srt backend sidecar.
#
# Build (from the repo root):
#   python -m PyInstaller packaging/backend.spec --noconfirm --clean
#
# Output: dist-backend/mai2srt-backend/  (ONEDIR -- a onefile build would
# re-unpack ~300 MB of playwright driver on EVERY launch; onedir pays the
# unpack once at install time).
#
# playwright needs its node driver (playwright/driver/*) bundled, hence
# collect_all. console=False: the sidecar must never flash a console
# window when the Tauri shell spawns it (streams are redirected to
# ~/.mai2srt/logs/backend.log by backend_entry.py).

from PyInstaller.utils.hooks import collect_all

# spec-relative paths: Analysis resolves them against the SPEC dir, not
# the invocation CWD
import os
ROOT = os.path.dirname(SPECPATH)

datas, binaries, hiddenimports = collect_all("playwright")

a = Analysis(
    [os.path.join(ROOT, "packaging", "backend_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports
    + [
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mai2srt-backend",
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="mai2srt-backend",
)
