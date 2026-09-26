# mai2srt release pipeline (mirrors the CI workflow step-for-step).
#
#   1. bump check          → all five version sites must agree
#   2. fetch_ffmpeg.ps1    → vendor\ffmpeg\{ffmpeg,ffprobe}.exe plus the
#                            GPLv3 notices (LICENSE.txt / README.txt / SOURCE.txt)
#   3. PyInstaller         → dist-backend\mai2srt-backend\ (onedir)
#   4. stage sidecar       → app\src-tauri\backend\
#                            (exe renamed with the target-triple suffix
#                            tauri externalBin resolution expects)
#   4b. stage notices      → app\src-tauri\backend\licenses\ (GPLv3 text,
#                            THIRD-PARTY-NOTICES.md, font OFL) -- runs even
#                            with -SkipBackend
#   5. tauri build         → NSIS installer
#   6. dist\ + SHA256SUMS
#
# Usage:
#   scripts\release.ps1
#   scripts\release.ps1 -FfmpegSourceDir D:\ffmpeg-9.0-full_build\bin
#   scripts\release.ps1 -SkipBackend   # frontend/NSIS/config iteration only
#
# -SkipBackend reuses the FROZEN backend already staged in
# app\src-tauri\backend\. Frontend (app\**) and Rust changes are still
# rebuilt by tauri build, but src\mai2srt\** Python changes are NOT --
# a package built that way silently ships the old backend. Any change
# under src\mai2srt\ requires a full run.

param(
    [string]$FfmpegSourceDir = "",
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"
$Root      = Split-Path $PSScriptRoot -Parent
$AppDir    = Join-Path $Root "app"
$TauriDir  = Join-Path $AppDir "src-tauri"
$StageDir  = Join-Path $TauriDir "backend"
$Triple    = "x86_64-pc-windows-msvc"
$DistDir   = Join-Path $Root "dist"

Write-Host "==> [1/6] version consistency check ..."
& (Join-Path $PSScriptRoot "bump.ps1") -Check
$Conf  = Get-Content (Join-Path $TauriDir "tauri.conf.json") -Raw | ConvertFrom-Json
$Version = $Conf.version
Write-Host "    version: $Version"

if (-not $SkipBackend) {
    Write-Host "==> [2/6] ffmpeg ..."
    $ffArgs = @{}
    if ($FfmpegSourceDir) { $ffArgs.SourceDir = $FfmpegSourceDir }
    & (Join-Path $Root "packaging\fetch_ffmpeg.ps1") @ffArgs

    Write-Host "==> [3/6] PyInstaller backend (onedir) ..."
    Push-Location $Root
    try {
        # PyInstaller logs its progress to STDERR, and under
        # ErrorActionPreference=Stop a native stderr line becomes a terminating
        # error -- which aborts the build mid-way for no reason, and does so
        # depending on how the caller redirected output. Same trap the tauri
        # step below guards against; scoping the preference is enough here
        # because PyInstaller's output is worth watching live.
        $eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            python -m PyInstaller packaging\backend.spec --noconfirm --clean --distpath dist-backend
        } finally {
            $ErrorActionPreference = $eap
        }
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }

    Write-Host "==> [4/6] staging sidecar into src-tauri\backend ..."
    $Built = Join-Path $Root "dist-backend\mai2srt-backend"
    if (-not (Test-Path (Join-Path $Built "mai2srt-backend.exe"))) {
        throw "missing $Built\mai2srt-backend.exe"
    }
    if (Test-Path $StageDir) { Remove-Item $StageDir -Recurse -Force }
    New-Item -ItemType Directory -Force $StageDir | Out-Null
    # exe gets the target-triple suffix tauri's externalBin lookup expects
    Copy-Item (Join-Path $Built "mai2srt-backend.exe") `
        (Join-Path $StageDir "mai2srt-backend-$Triple.exe")
    Copy-Item (Join-Path $Built "_internal") $StageDir -Recurse
    New-Item -ItemType Directory -Force (Join-Path $StageDir "ffmpeg") | Out-Null
    # every file, not just the two exes: LICENSE.txt / README.txt / SOURCE.txt
    # are the GPLv3 paperwork that has to ship beside the binaries
    Copy-Item (Join-Path $Root "vendor\ffmpeg\*") (Join-Path $StageDir "ffmpeg")
} else {
    Write-Host "==> [2-4/6] -SkipBackend: reusing $StageDir"
    if (-not (Test-Path (Join-Path $StageDir "mai2srt-backend-$Triple.exe"))) {
        throw "staged sidecar missing; run once without -SkipBackend"
    }
}

# license notices travel with the installer. Staged outside the backend branch
# on purpose: they are repo files, so -SkipBackend builds must ship them too.
Write-Host "==> [4b/6] license notices ..."
$LicDir = Join-Path $StageDir "licenses"
if (Test-Path $LicDir) { Remove-Item $LicDir -Recurse -Force }
New-Item -ItemType Directory -Force $LicDir | Out-Null
Copy-Item (Join-Path $Root "LICENSE") (Join-Path $LicDir "LICENSE")
Copy-Item (Join-Path $Root "THIRD-PARTY-NOTICES.md") $LicDir
Copy-Item (Join-Path $Root "licenses\*.txt") $LicDir
Get-ChildItem $LicDir | ForEach-Object { Write-Host "    licenses\$($_.Name)" }

# the ffmpeg notices come from vendor\ffmpeg (fetched, not built), so refresh
# them here as well -- the sidecar branch above only runs without -SkipBackend
$FfmpegStage = Join-Path $StageDir "ffmpeg"
New-Item -ItemType Directory -Force $FfmpegStage | Out-Null
Copy-Item (Join-Path $Root "vendor\ffmpeg\*.txt") $FfmpegStage
foreach ($f in @("LICENSE.txt", "README.txt", "SOURCE.txt")) {
    if (-not (Test-Path (Join-Path $FfmpegStage $f))) {
        Write-Warning "ffmpeg\$f missing from the stage -- GPLv3 notices will be incomplete (run packaging\fetch_ffmpeg.ps1)"
    }
}

Write-Host "==> [5/6] tauri build (frontend + NSIS) ..."
Push-Location $AppDir
try {
    # invoke the tauri CLI through the real node binary, never the pnpm
    # shim (its host-Electron execPath breaks spawn quoting). Run via cmd
    # with a native stderr redirect: the CLI prints INFO lines to stderr,
    # and PowerShell's own 2> wraps them into ErrorRecords that
    # ErrorActionPreference=Stop would then surface as failures
    & cmd /c "node `"node_modules\@tauri-apps\cli\tauri.js`" build 2>`"$env:TEMP\mai2srt-tauri-build.log`""
    if ($LASTEXITCODE -ne 0) {
        Get-Content "$env:TEMP\mai2srt-tauri-build.log" -Tail 20
        throw "tauri build failed (exit $LASTEXITCODE)"
    }
} finally {
    Pop-Location
}

Write-Host "==> [6/6] collecting artifacts + checksums ..."
$NsisDir = Join-Path $TauriDir "target\release\bundle\nsis"
$Setup = Get-ChildItem $NsisDir -Filter "*-setup.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $Setup) { throw "no NSIS setup exe under $NsisDir" }

if (Test-Path $DistDir) { Remove-Item $DistDir -Recurse -Force }
New-Item -ItemType Directory -Force $DistDir | Out-Null
Copy-Item $Setup.FullName $DistDir

$sums = Get-ChildItem $DistDir -File | Where-Object { $_.Name -ne "SHA256SUMS.txt" } | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    "$hash  $($_.Name)"
}
$sums | Out-File (Join-Path $DistDir "SHA256SUMS.txt") -Encoding ascii

Write-Host ""
Write-Host "==> release $Version artifacts:"
Get-ChildItem $DistDir -File | ForEach-Object {
    Write-Host ("    {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB))
}
