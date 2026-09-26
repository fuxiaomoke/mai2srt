# Fetch ffmpeg.exe + ffprobe.exe into vendor\ffmpeg\ for packaging.
#
# Two sources:
#   -SourceDir D:\ffmpeg-9.0-full_build\bin   # copy from a local install
#   (default)                                  # pinned gyan.dev download
#
# The pinned URL carries a SHA-256: a pruned/changed upstream package fails
# LOUDLY here instead of shipping untested binaries. When gyan.dev prunes
# the pinned version, bump $Version + $ExpectedSha256 together (compute the
# hash after one manual download).

param(
    [string]$Version = "9.0.2",
    [string]$Url = "",
    # gyan.dev ffmpeg-9.0.2-essentials_build.zip (2026-09-25 fetch)
    [string]$ExpectedSha256 = "60F467265B1E312373DBCD92200C2618A74850F98D3D078E94296BB3FA2047BA",
    [string]$SourceDir = "",
    # upstream source of the pinned build. gyan.dev's packages are GPLv3, so the
    # license text and a pointer to the exact source must ship with the binaries;
    # both are recorded into vendor\ffmpeg\SOURCE.txt below.
    [string]$SourceTarUrl = "https://ffmpeg.org/releases/ffmpeg-$Version.tar.xz",
    [string]$SourceCommit = "https://github.com/FFmpeg/FFmpeg/commit/946fcce07b"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Out  = Join-Path $Root "vendor\ffmpeg"

if (-not $Url) { $Url = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-$Version-essentials_build.zip" }

# the notices are part of the payload: a cache that only has the exes is
# incomplete, so treat them as required and re-fetch when they are missing
$Required = @("ffmpeg.exe", "ffprobe.exe", "LICENSE.txt", "README.txt", "SOURCE.txt")
$Missing  = @($Required | Where-Object { -not (Test-Path (Join-Path $Out $_)) })
if ($Missing.Count -eq 0) {
    Write-Host "==> vendor\ffmpeg already populated, skipping"
    exit 0
}

function Write-SourceNote([string]$PackageRoot) {
    $lines = @(
        "FFmpeg $Version -- bundled with mai2srt",
        "",
        "These binaries are licensed under the GNU General Public License v3.",
        "The full license text is in LICENSE.txt next to this file.",
        "",
        "Package   : ffmpeg-$Version-essentials_build (gyan.dev)",
        "Fetched   : $Url",
        "SHA-256   : $($ExpectedSha256.ToLower())",
        "Source    : $SourceTarUrl",
        "Upstream  : $SourceCommit",
        "",
        "The build is unmodified upstream FFmpeg; the exact configure line of the",
        "shipped binaries is in README.txt next to this file."
    )
    ($lines -join "`r`n") + "`r`n" | Set-Content (Join-Path $Out "SOURCE.txt") -Encoding ascii
    if ($PackageRoot) {
        foreach ($pair in @(@("LICENSE", "LICENSE.txt"), @("README.txt", "README.txt"))) {
            $src = Join-Path $PackageRoot $pair[0]
            if (Test-Path $src) { Copy-Item $src (Join-Path $Out $pair[1]) -Force }
        }
    }
}

if ($SourceDir) {
    Write-Host "==> copying ffmpeg from $SourceDir"
    New-Item -ItemType Directory -Force $Out | Out-Null
    Copy-Item (Join-Path $SourceDir "ffmpeg.exe")  $Out
    Copy-Item (Join-Path $SourceDir "ffprobe.exe") $Out
    # a local ffmpeg install keeps the notices one level up (the extracted
    # package root, not bin\)
    Write-SourceNote (Split-Path $SourceDir -Parent)
    foreach ($f in @("LICENSE.txt", "README.txt")) {
        if (-not (Test-Path (Join-Path $Out $f))) {
            Write-Warning "$f not found near $SourceDir -- GPLv3 notices will be incomplete in the installer. Prefer the default download, or copy the file in by hand."
        }
    }
} else {
    $Zip = Join-Path $Root ("vendor\ffmpeg-$Version-essentials_build.zip")
    if (-not (Test-Path $Zip)) {
        Write-Host "==> downloading $Url"
        # vendor\ is gitignored, so it does not exist in a fresh clone or a CI
        # checkout: create it before writing the zip. Without this the download
        # dies with DirectoryNotFoundException on any machine that has never
        # built before, while the maintainer's copy keeps working.
        New-Item -ItemType Directory -Force (Split-Path $Zip -Parent) | Out-Null
        Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
    }
    if ($ExpectedSha256) {
        $actual = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $ExpectedSha256.ToLower()) {
            Remove-Item $Zip -Force
            throw "ffmpeg zip sha256 mismatch:`n  expected $ExpectedSha256`n  actual   $actual`n(deleted the zip; re-pin the version+hash in packaging/fetch_ffmpeg.ps1)"
        }
    } else {
        Write-Warning "no -ExpectedSha256 given: shipping UNVERIFIED binaries (ok for local tests only)"
    }
    Write-Host "==> extracting ffmpeg.exe / ffprobe.exe + license notices"
    $Stage = Join-Path $Root "vendor\ffmpeg-extract"
    if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
    Expand-Archive $Zip $Stage
    $bin = Get-ChildItem $Stage -Recurse -Directory -Filter "bin" | Select-Object -First 1
    if (-not $bin) { throw "bin/ not found inside the ffmpeg zip" }
    New-Item -ItemType Directory -Force $Out | Out-Null
    Copy-Item (Join-Path $bin.FullName "ffmpeg.exe")  $Out
    Copy-Item (Join-Path $bin.FullName "ffprobe.exe") $Out
    Write-SourceNote (Split-Path $bin.FullName -Parent)
    Remove-Item $Stage -Recurse -Force
}

Get-ChildItem $Out | ForEach-Object { Write-Host ("    {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) }
