# Version single-source management. The version lives in FIVE places and
# they must never drift (the installer name, the backend /api/system
# version and the Python package all read different files):
#
#   pyproject.toml                    [project] version
#   src/mai2srt/__init__.py           __version__
#   app/package.json                  "version"
#   app/src-tauri/tauri.conf.json     "version"
#   app/src-tauri/Cargo.toml          [package] version
#
# Usage:
#   scripts\bump.ps1 0.2.0        # set all five
#   scripts\bump.ps1 -Check       # verify all five agree (CI gate)

param(
    [string]$Version = "",
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

# Line endings: .NET's `$` does not match before CRLF, so a checkout with
# core.autocrlf=true (the Git for Windows default, and what a GitHub runner
# does) made every pattern below miss and this script abort the release with
# "version field not found". `\r?$` accepts both; .gitattributes pins LF so
# the working tree no longer depends on the machine's autocrlf setting.
$Targets = @(
    @{ File = "pyproject.toml";                Pattern = '(?m)^(version = ")[^"]+(")\r?$' },
    @{ File = "src\mai2srt\__init__.py";       Pattern = '(?m)^(__version__ = ")[^"]+(")\r?$' },
    @{ File = "app\package.json";              Pattern = '(?m)^(\s*"version": ")[^"]+(",?)\r?$' },
    @{ File = "app\src-tauri\tauri.conf.json"; Pattern = '(?m)^(\s*"version": ")[^"]+(",?)\r?$' },
    @{ File = "app\src-tauri\Cargo.toml";      Pattern = '(?m)^(version = ")[^"]+(")\r?$' }
)

function Read-Version($file, $pattern) {
    $text = [System.IO.File]::ReadAllText((Join-Path $Root $file))
    $m = [regex]::Match($text, $pattern)
    if (-not $m.Success) { throw "version field not found in $file" }
    return $m.Groups[0].Value -replace '[^0-9.]', ''
}

if ($Check) {
    $versions = $Targets | ForEach-Object { @{ $_.File = (Read-Version $_.File $_.Pattern) } }
    $unique = @(($versions | ForEach-Object { $_.Values }) | Sort-Object -Unique)
    $versions | ForEach-Object { $_.GetEnumerator() | ForEach-Object { Write-Host ("    {0,-34} {1}" -f $_.Key, $_.Value) } }
    if ($unique.Count -ne 1) { throw "version drift detected (see above)" }
    Write-Host "==> all versions agree: $($unique -join '')"
    exit 0
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "usage: bump.ps1 <x.y.z> (semver, no leading v)" }

foreach ($t in $Targets) {
    $path = Join-Path $Root $t.File
    $cur = Read-Version $t.File $t.Pattern
    if ($cur -eq $Version) { Write-Host "    $($t.File) already $Version"; continue }
    $text = [System.IO.File]::ReadAllText($path)
    $new = [regex]::Replace($text, $t.Pattern, "`${1}$Version`${2}", 1)
    if ($new -eq $text) { throw "no replacement made in $($t.File) (pattern drifted?)" }
    [System.IO.File]::WriteAllText($path, $new, [System.Text.UTF8Encoding]::new($false))
    Write-Host "    $($t.File) -> $Version"
}
Write-Host "==> bumped to $Version; commit, then tag v$Version to trigger the release CI"
