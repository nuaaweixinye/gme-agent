function Resolve-AgentPython {
    param([string]$Python = "")

    $command = if ($Python) {
        Get-Command $Python -ErrorAction Stop
    } else {
        Get-Command python -ErrorAction Stop
    }
    $executable = $command.Source
    if (-not $executable) {
        $executable = $command.Path
    }
    if (-not $executable) {
        throw "Unable to resolve a Python executable. Activate a Python environment or pass -Python <path>."
    }

    & $executable -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.10 or newer is required: $executable"
    }
    return $executable
}

function Assert-AgentPythonDependencies {
    param([Parameter(Mandatory = $true)][string]$Python)

    & $Python -c "import deepseek_harness" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "DeepSeek Harness Python SDK is missing. Run: scripts\install.ps1 -GmeRepo <path>"
    }
}

$HarnessSdkVersion = "0.1.2a5"
$HarnessReleaseTag = "harness-sdk-$HarnessSdkVersion"
$HarnessRepoSlug = "nuaaweixinye/gme-agent"

function Get-HarnessWheelNames {
    return @(
        "deepseek_harness_sdk-$HarnessSdkVersion-py3-none-any.whl"
        "deepseek_harness_runtime_bin-$HarnessSdkVersion-py3-none-win_amd64.whl"
    )
}

# The two DeepSeek Harness wheels are pinned to a build that is not on PyPI
# (see docs/harness-sdk-wheels.md), so they come from this repository's
# release. Direct URLs first, `gh release download` as the fallback.
function Install-AgentHarnessWheels {
    param([Parameter(Mandatory = $true)][string]$Python)

    $names = Get-HarnessWheelNames
    $urls = foreach ($name in $names) {
        "https://github.com/$HarnessRepoSlug/releases/download/$HarnessReleaseTag/$name"
    }

    Write-Host "Installing the pinned DeepSeek Harness wheels ($HarnessSdkVersion)..."
    & $Python -m pip install --disable-pip-version-check --retries 5 --timeout 60 @urls
    if ($LASTEXITCODE -eq 0) {
        return
    }

    if (Get-Command gh -ErrorAction SilentlyContinue) {
        Write-Host "Direct download failed; falling back to gh release download."
        $dir = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..")) ".wheels"
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        & gh release download $HarnessReleaseTag --repo $HarnessRepoSlug --dir $dir --clobber
        if ($LASTEXITCODE -ne 0) {
            throw "Could not download the harness wheels. Fetch them manually from https://github.com/$HarnessRepoSlug/releases/tag/$HarnessReleaseTag"
        }
        $paths = foreach ($name in $names) { Join-Path $dir $name }
        & $Python -m pip install --disable-pip-version-check @paths
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to install the harness wheels from $dir"
        }
        return
    }

    throw "Could not install the harness wheels. Fetch them from https://github.com/$HarnessRepoSlug/releases/tag/$HarnessReleaseTag and install them with pip."
}
