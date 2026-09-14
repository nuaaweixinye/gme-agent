param(
    [ValidateSet("portable", "unpacked")]
    [string]$Target = "portable",
    [string]$Python = "",
    [string]$CondaEnv = "",
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not $SkipBackend) {
    & (Join-Path $PSScriptRoot "package_backend.ps1") -Python $Python -CondaEnv $CondaEnv
}

Set-Location (Join-Path $Root "frontend")

$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
$env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"

# npm.cmd avoids the argument-mangling bug in Node 22+'s npm.ps1 shim.
if (-not (Test-Path "node_modules")) {
    & npm.cmd ci
}

if ($Target -eq "portable") {
    & npm.cmd run electron:portable
} else {
    & npm.cmd run electron:pack
}
