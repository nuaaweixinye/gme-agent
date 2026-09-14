param(
    [string]$Python = "",
    [string]$CondaEnv = ""
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
. (Join-Path $PSScriptRoot "python_runtime.ps1")
Set-Location $Root

if ($CondaEnv) {
    & conda run -n $CondaEnv python -m unittest discover -s tests
} else {
    $PythonExe = Resolve-AgentPython -Python $Python
    Assert-AgentPythonDependencies -Python $PythonExe
    & $PythonExe -m unittest discover -s tests
}
