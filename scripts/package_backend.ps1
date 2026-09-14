param(
    [string]$Python = "",
    [string]$CondaEnv = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "python_runtime.ps1")
$RuntimeRoot = Join-Path $Root "backend-runtime"
$PyInstallerRoot = Join-Path $Root "artifacts\pyinstaller"
$BuildRoot = Join-Path $PyInstallerRoot "build"
$SpecRoot = Join-Path $PyInstallerRoot "spec"
$Entry = Join-Path $Root "backend\run_backend.py"
$BackendPath = Join-Path $Root "backend"

function Invoke-BackendPython {
    param([string[]]$Arguments)
    if ($Python) {
        $PythonExe = Resolve-AgentPython -Python $Python
        & $PythonExe @Arguments
    } elseif ($CondaEnv) {
        & conda run -n $CondaEnv python @Arguments
    } else {
        $PythonExe = Resolve-AgentPython
        & $PythonExe @Arguments
    }
}

if (-not $SkipInstall) {
    Invoke-BackendPython @("-m", "pip", "install", "-r", (Join-Path $Root "requirements.txt"))
    Invoke-BackendPython @("-m", "pip", "install", "pyinstaller")
}

if (Test-Path $RuntimeRoot) {
    Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $RuntimeRoot, $BuildRoot, $SpecRoot | Out-Null

Invoke-BackendPython @(
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name",
    "gme-agent-backend",
    "--distpath",
    $RuntimeRoot,
    "--workpath",
    $BuildRoot,
    "--specpath",
    $SpecRoot,
    "--paths",
    $BackendPath,
    "--collect-submodules",
    "gme_agent",
    "--add-data",
    "$BackendPath\gme_agent\interface_catalog\catalogs;gme_agent/interface_catalog/catalogs",
    "--collect-all",
    "openai_codex",
    "--collect-all",
    "codex_cli_bin",
    "--copy-metadata",
    "openai-codex",
    "--copy-metadata",
    "openai-codex-cli-bin",
    $Entry
)

$Exe = Join-Path $RuntimeRoot "gme-agent-backend\gme-agent-backend.exe"
if (-not (Test-Path $Exe)) {
    throw "Backend runtime was not created: $Exe"
}

Write-Host "Backend runtime created: $Exe"
