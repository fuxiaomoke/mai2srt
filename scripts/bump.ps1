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
# A SIXTH copy is derived, not authoritative: Cargo.lock records our own
# package version, and cargo rewrites it from Cargo.toml the next time it
# runs. Left alone it made the tree dirty minutes before a publish, which
# publish.ps1 then refused ("uncommitted changes") with nothing actually
# wrong -- so it is bumped here too, and -Check reports it as a reminder
# rather than as part of the "must agree" set.
#
# Usage:
#   scripts\bump.ps1 0.2.0        # set all five (+ the Cargo.lock copy)
#   scripts\bump.ps1 -Check       # verify all five agree (CI gate)

param(
    [string]$Version = "",
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

# The derived copy (see the header). It is anchored on our own package name, so
# the pattern cannot hit a dependency's entry: `^name = "mai2srt"` then the
# `version` line right below it. Group 2 is the value, but the generic
# "${1}${2}" replacement below still works because the pattern only exposes
# prefix/suffix groups around it.
$LockFile = "app\src-tauri\Cargo.lock"
$LockPattern = '(?m)(^name = "mai2srt"\r?\nversion = ")[^"]+(")'

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
    # the derived copy is reported, not enforced: it belongs to cargo, and a
    # checkout where cargo has not run yet is not a version drift
    $lockPath = Join-Path $Root $LockFile
    if (Test-Path $lockPath) {
        $m = [regex]::Match([System.IO.File]::ReadAllText($lockPath), $LockPattern)
        if ($m.Success) {
            $lockVersion = ($m.Groups[0].Value -split '"')[-2]
            $note = if ($lockVersion -ne $unique[0]) { " (stale; run bump.ps1 $($unique[0]) to sync)" } else { "" }
            Write-Host ("    {0,-34} {1}{2}" -f $LockFile, $lockVersion, $note)
        }
    }
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

# keep the derived copy in step, or the tree is dirty right when it must not be
$lockPath = Join-Path $Root $LockFile
if (Test-Path $lockPath) {
    $text = [System.IO.File]::ReadAllText($lockPath)
    $new = [regex]::Replace($text, $LockPattern, "`${1}$Version`${2}", 1)
    if ($new -ne $text) {
        [System.IO.File]::WriteAllText($lockPath, $new, [System.Text.UTF8Encoding]::new($false))
        Write-Host "    $LockFile -> $Version"
    }
}
Write-Host "==> bumped to $Version; commit, then tag v$Version to trigger the release CI"
