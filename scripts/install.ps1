<#
.SYNOPSIS
    One-command setup for the GME Test Agent backend on Windows.

.DESCRIPTION
    Checks the toolchain, creates a virtual environment, installs the two pinned
    DeepSeek Harness wheels (they are published on this repository's
    harness-sdk-0.1.2a5 release because they are not on PyPI — see
    docs/harness-sdk-wheels.md), installs requirements.txt, writes
    config.local.json, verifies the result and prints the cordis patch snippet
    that points the dsh-gme-workflow plugin at this checkout.

    Re-running is safe: the virtual environment, an existing config.local.json
    and downloaded wheels are reused.

.PARAMETER GmeRepo
    Path to the GME superproject checkout under test. Required.

.PARAMETER DshHome
    DeepSeek Harness home holding the credentials the coding sessions use.
    Defaults to ~/.dsh.

.PARAMETER DshProfile
    Harness profile the coding sessions run in. Defaults to sdk.

.PARAMETER Python
    Interpreter used to create the virtual environment. Defaults to python on PATH.

.PARAMETER SkipFrontend
    Skip the frontend dependency install (npm ci).

.PARAMETER SkipWheels
    Assume the two DeepSeek Harness wheels are already installed.

.PARAMETER RecreateConfig
    Overwrite an existing config.local.json from config.example.json.

.EXAMPLE
    scripts\install.ps1 -GmeRepo D:\GME
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$GmeRepo,
    [string]$DshHome = "",
    [string]$DshProfile = "sdk",
    [string]$Python = "",
    [switch]$SkipFrontend,
    [switch]$SkipWheels,
    [switch]$RecreateConfig
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "python_runtime.ps1")

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$ConfigPath = Join-Path $Root "config.local.json"
$Warnings = New-Object System.Collections.Generic.List[string]

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Add-Warning([string]$Text) {
    $Warnings.Add($Text)
    Write-Host "    ! $Text" -ForegroundColor Yellow
}

# Resolve the interpreter used to create .venv: an explicit -Python wins, then a
# suitable `python`/`python3`, then the Windows `py` launcher. Machines often put
# an old interpreter first on PATH (a conda base environment, for instance), so
# every candidate is version-checked instead of trusting the first hit.
function Resolve-InstallPython([string]$Requested) {
    $candidates = @()
    if ($Requested) {
        $candidates += , @($Requested)
    } else {
        $candidates += , @("python")
        $candidates += , @("python3")
        if (Get-Command py -ErrorAction SilentlyContinue) {
            foreach ($version in @("-3.13", "-3.12", "-3.11", "-3.10")) {
                $candidates += , @("py", $version)
            }
        }
    }

    $rejected = @()
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $exe = if ($command.Source) { $command.Source } else { $command.Path }
        $args = @()
        if ($candidate.Count -gt 1) { $args = $candidate[1..($candidate.Count - 1)] }
        $major = & $exe @args -c "import sys; print(sys.version_info[0])" 2>$null
        $minor = & $exe @args -c "import sys; print(sys.version_info[1])" 2>$null
        $label = "$exe $($args -join ' ')".Trim()
        if ($LASTEXITCODE -ne 0) { $rejected += "$label (not runnable)"; continue }
        if ([int]$major -gt 3 -or ([int]$major -eq 3 -and [int]$minor -ge 10)) {
            return @{ Exe = $exe; Args = $args; Label = $label }
        }
        $rejected += "$label (Python $major.$minor, needs 3.10+)"
    }

    $message = "No Python 3.10+ interpreter found."
    if ($rejected.Count -gt 0) { $message += " Tried: " + ($rejected -join "; ") + "." }
    $message += " Pass one explicitly, for example: scripts\install.ps1 -GmeRepo <path> -Python C:\path\to\python.exe"
    throw $message
}

# ---------------------------------------------------------------- toolchain --
Write-Step "Checking the toolchain"

if ($env:OS -ne "Windows_NT") {
    Add-Warning "This backend is Windows-oriented; the harness runtime wheel is win_amd64 only."
}

$BasePython = Resolve-InstallPython -Requested $Python
$BasePythonExe = $BasePython.Exe
$BasePythonArgs = $BasePython.Args
Write-Host "    python : $($BasePython.Label)"

if (-not $SkipFrontend) {
    foreach ($tool in @("node", "npm")) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            throw "Required tool is not on PATH: $tool (install Node.js 18+, or pass -SkipFrontend)"
        }
    }
    $nodeMajor = [int]((& node --version).TrimStart("v").Split(".")[0])
    if ($nodeMajor -lt 18) {
        throw "Node.js 18 or newer is required for the frontend; found $nodeMajor (or pass -SkipFrontend)."
    }
    Write-Host "    node   : $(& node --version)"
}

if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "    git    : $(& git --version)"
} else {
    Add-Warning "git is not on PATH; the backend cannot create worktrees or diff without it."
}

if (Get-Command gh -ErrorAction SilentlyContinue) {
    & gh auth status *> $null
    if ($LASTEXITCODE -ne 0) {
        Add-Warning "gh is installed but not authenticated; run 'gh auth status' and 'gh auth login' before opening PRs."
    } else {
        Write-Host "    gh     : authenticated"
    }
} else {
    Add-Warning "gh is not on PATH; the PR steps of the workflow will not work."
}

$GmeRepoFull = [System.IO.Path]::GetFullPath($GmeRepo)
if (Test-Path -LiteralPath $GmeRepoFull -PathType Container) {
    Write-Host "    GME    : $GmeRepoFull"
} else {
    Add-Warning "GME checkout not found at $GmeRepoFull — fix config.local.json before running tasks."
}

if ($DshHome -eq "") {
    $DshHome = Join-Path $HOME ".dsh"
}
if (-not (Test-Path -LiteralPath $DshHome -PathType Container)) {
    Add-Warning "Harness home '$DshHome' does not exist yet; the coding SDK reads its credentials from there."
}

# ------------------------------------------------------------ environment --
Write-Step "Preparing the virtual environment"

if (Test-Path -LiteralPath $VenvPython) {
    Write-Host "    reusing $VenvDir"
} else {
    & $BasePythonExe @BasePythonArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment at $VenvDir" }
    Write-Host "    created $VenvDir"
}
& $VenvPython -m pip install --disable-pip-version-check --quiet --upgrade pip | Out-Null

# ------------------------------------------------------------- dependencies --
Write-Step "Installing Python dependencies"

if ($SkipWheels) {
    Write-Host "    skipping the harness wheels (-SkipWheels)"
} else {
    Install-AgentHarnessWheels -Python $VenvPython
}

& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install requirements.txt. The two harness wheels must be installed first — see docs/harness-sdk-wheels.md."
}
$sdkVersion = (& $VenvPython -c "import importlib.metadata as m; print(m.version('deepseek-harness-sdk'))" 2>$null)
$runtimeVersion = (& $VenvPython -c "import importlib.metadata as m; print(m.version('deepseek-harness-runtime-bin'))" 2>$null)
Write-Host "    deepseek-harness-sdk        $sdkVersion"
Write-Host "    deepseek-harness-runtime-bin $runtimeVersion"

if (-not $SkipFrontend) {
    Write-Step "Installing frontend dependencies"
    Push-Location (Join-Path $Root "frontend")
    try {
        $env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
        $env:ELECTRON_BUILDER_BINARIES_MIRROR = "https://npmmirror.com/mirrors/electron-builder-binaries/"
        # The npm.ps1 shim shipped with Node 22+ mangles arguments through the
        # call operator ("& npm ci" becomes "pm ci"); npm.cmd is unaffected.
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw "Failed to install frontend dependencies." }
    } finally {
        Pop-Location
    }
}

# ------------------------------------------------------------ configuration --
Write-Step "Writing config.local.json"

if ((Test-Path -LiteralPath $ConfigPath) -and -not $RecreateConfig) {
    Write-Host "    keeping the existing $ConfigPath (pass -RecreateConfig to rebuild it)"
    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
} else {
    $config = Get-Content -LiteralPath (Join-Path $Root "config.example.json") -Raw | ConvertFrom-Json
    $config.worktree_root = (Join-Path $Root "worktrees").Replace("\", "/")
    $config.artifact_root = (Join-Path $Root "artifacts").Replace("\", "/")
    $config.database_path = (Join-Path $Root "gme_agent.db").Replace("\", "/")
    Write-Host "    created from config.example.json"
}
$config.gme_repo_path = $GmeRepoFull.Replace("\", "/")
$config.dsh_home = ([System.IO.Path]::GetFullPath($DshHome)).Replace("\", "/")
$config.dsh_profile = $DshProfile
[System.IO.File]::WriteAllText($ConfigPath, (($config | ConvertTo-Json -Depth 10) + "`n"), [System.Text.UTF8Encoding]::new($false))
Write-Host "    gme_repo_path : $($config.gme_repo_path)"
Write-Host "    dsh_home      : $($config.dsh_home)  (profile: $($config.dsh_profile))"

# ------------------------------------------------------------------ verify --
Write-Step "Verifying the installation"

& $VenvPython -c "import deepseek_harness; print('    DeepSeek Harness SDK import: OK')"
if ($LASTEXITCODE -ne 0) { throw "The DeepSeek Harness SDK does not import in $VenvDir" }

$catalogRoot = Join-Path $Root "backend\gme_agent\interface_catalog\catalogs"
$catalogs = @(Get-ChildItem -LiteralPath $catalogRoot -Filter *.json -ErrorAction SilentlyContinue)
if ($catalogs.Count -eq 0) {
    Write-Host "    interface catalogs: none yet (expected on a fresh clone)"
} else {
    Write-Host "    interface catalogs: $($catalogs.Count) module(s): $(($catalogs.BaseName | Sort-Object) -join ', ')"
}

$port = if ($config.PSObject.Properties.Name -contains "port") { $config.port } else { 8765 }
try {
    $probe = New-Object System.Net.Sockets.TcpClient
    $probe.Connect("127.0.0.1", 8765)
    $probe.Close()
    Write-Host "    port 8765: something is already listening (an existing backend is reused)"
} catch {
    Write-Host "    port 8765: free (autoStart can start the backend)"
}

# -----------------------------------------------------------------  summary --
Write-Step "Done"

if ($Warnings.Count -gt 0) {
    Write-Host "Review these warnings:" -ForegroundColor Yellow
    foreach ($warning in $Warnings) { Write-Host "  - $warning" -ForegroundColor Yellow }
    Write-Host ""
}

if ($catalogs.Count -eq 0) {
    Write-Host "Next, generate the module interface catalogs from your GME checkout:"
    Write-Host "  `"$VenvPython`" scripts\generate_interface_catalog.py --gme-root `"$($config.gme_repo_path)`" --acis-symbol-dir <ACIS symbol CSV directory> --module base --module kernel --module laws"
    Write-Host ""
}

Write-Host "Start the backend:"
Write-Host "  scripts\run_web.ps1"
Write-Host ""
Write-Host "Then point the DeepSeek Harness plugin at this checkout. Either export these before starting Harness:"
Write-Host "  `$env:GME_TEST_AGENT_ROOT   = '$($Root.Replace('\', '/'))'"
Write-Host "  `$env:GME_TEST_AGENT_PYTHON = '$($VenvPython.Replace('\', '/'))'"
Write-Host ""
Write-Host "or override the row in `$DSH_HOME/profiles/web/cordis.patch.yml (the `disabled: false` line is required:"
Write-Host "a config-only override keeps the guard the plugin ships with):"
Write-Host ""
Write-Host "  - id: gme-workflow"
Write-Host "    disabled: false"
Write-Host "    config:"
Write-Host "      backendRoot: $($Root.Replace('\', '/'))"
Write-Host "      pythonPath: $($VenvPython.Replace('\', '/'))"
Write-Host "      port: 8765"
Write-Host "      autoStart: true"
Write-Host ""
Write-Host "Verify with scripts\run_tests.ps1 (expects 'OK (skipped=N)' when no interface catalogs exist yet)."
