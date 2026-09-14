param(
    [string]$Python = "",
    [string]$CondaEnv = "",
    [int]$Port = 8765,
    [Parameter(Mandatory = $true)]
    [string]$ApiToken
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "python_runtime.ps1")
Set-Location $Root
$env:GME_AGENT_API_TOKEN = $ApiToken

if ($CondaEnv) {
    & conda run -n $CondaEnv python backend/run_backend.py --config config.local.json --host 127.0.0.1 --port $Port
} else {
    $PythonExe = Resolve-AgentPython -Python $Python
    Assert-AgentPythonDependencies -Python $PythonExe
    & $PythonExe backend/run_backend.py --config config.local.json --host 127.0.0.1 --port $Port
}
