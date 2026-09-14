param(
    [string]$Python = "",
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "python_runtime.ps1")
Set-Location $Root

$PythonExe = Resolve-AgentPython -Python $Python
Write-Host "Using Python: $PythonExe"
# The pinned harness wheels are not on PyPI; install them from the release
# before requirements.txt, which would otherwise fail to resolve them.
Install-AgentHarnessWheels -Python $PythonExe
& $PythonExe -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Python dependencies."
}

if (-not $SkipFrontend) {
    foreach ($tool in ("node", "npm")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "Required tool is not on PATH: $tool"
        }
    }
    $nodeMajor = [int]((& node --version).TrimStart("v").Split(".")[0])
    if ($nodeMajor -lt 18) {
        throw "Node.js 18 or newer is required."
    }
    Push-Location (Join-Path $Root "frontend")
    try {
        # Same mirrors as package_desktop.ps1: electron's postinstall downloads
        # from GitHub, which fails with ECONNRESET on unreliable connections.
        $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
        $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"
        # The npm.ps1 shim shipped with Node 22+ mangles arguments when invoked
        # through the call operator ("& npm ci" becomes "pm ci"); use npm.cmd.
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install frontend dependencies."
        }
    } finally {
        Pop-Location
    }
}

$ConfigPath = Join-Path $Root "config.local.json"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    $config = Get-Content -LiteralPath (Join-Path $Root "config.example.json") -Raw | ConvertFrom-Json
    $config.worktree_root = (Join-Path $Root "worktrees").Replace("\", "/")
    $config.artifact_root = (Join-Path $Root "artifacts").Replace("\", "/")
    $config.database_path = (Join-Path $Root "gme_agent.db").Replace("\", "/")
    $json = $config | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($ConfigPath, "$json`n", [Text.UTF8Encoding]::new($false))
    Write-Host "Created config.local.json. Update gme_repo_path before starting the application."
} else {
    Write-Host "Keeping existing config.local.json."
}

$missing = @()
foreach ($tool in ("git", "cmake", "clang-format", "gh")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        $missing += $tool
    }
}

$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$visualStudio = ""
if (Test-Path -LiteralPath $vswhere) {
    $visualStudio = & $vswhere -latest -version "[17.0,18.0)" -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
}
if (-not $visualStudio) {
    $missing += "Visual Studio 2022 C++ Build Tools"
}

$codexAuth = [bool]$env:OPENAI_API_KEY -or (Test-Path -LiteralPath (Join-Path $HOME ".codex\auth.json"))
if (-not $codexAuth) {
    $missing += "Codex login"
}

if ($missing.Count) {
    Write-Warning ("Setup completed, but required external tools are missing: " + ($missing -join ", "))
    Write-Warning "Install or configure them, then use the application's environment check."
} else {
    Write-Host "External tool check passed."
}

Write-Host "Source setup completed. Next: update config.local.json, then run scripts\run_web.ps1."
