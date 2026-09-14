$ErrorActionPreference = "Stop"

$stopped = @{}

function Stop-GmeBackendProcess {
    param([int]$ProcessId)

    if ($stopped.ContainsKey($ProcessId)) {
        return
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) {
        return
    }
    if ($process.CommandLine -match "run_backend\.py") {
        Write-Host "Stopping backend process $ProcessId"
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        $stopped[$ProcessId] = $true
    } else {
        Write-Host "Port 8765 is used by process $ProcessId, but it does not look like GME backend."
        Write-Host $process.CommandLine
    }
}

$connections = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
foreach ($connection in $connections) {
    Stop-GmeBackendProcess -ProcessId $connection.OwningProcess
}

$backendProcesses = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "run_backend\.py" }
foreach ($process in $backendProcesses) {
    Stop-GmeBackendProcess -ProcessId $process.ProcessId
}

if ($stopped.Count -eq 0) {
    Write-Host "No GME Test Agent backend process found."
}
