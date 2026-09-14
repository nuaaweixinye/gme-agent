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

    & $Python -c "import openai_codex" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependencies are missing. Run: `"$Python`" -m pip install -r requirements.txt"
    }
}
