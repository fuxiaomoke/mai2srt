# mai2srt app dev launcher (one command for the whole app).
#
# Runs the Tauri CLI through the local .bin cmd shim on purpose: under the
# DSH host, `pnpm` on PATH resolves to a host shim whose node is the host
# Electron binary (a path with spaces), which corrupts spawn argument
# quoting ("unrecognized subcommand '...DSH Desktop.exe'"). The cmd shim
# resolves the real node on PATH, which is clean.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& ".\node_modules\.bin\tauri.cmd" dev
