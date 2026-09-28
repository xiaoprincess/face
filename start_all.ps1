$ErrorActionPreference = "Stop"

$faceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$webRoot = Join-Path $faceRoot "unified_web_app"
$mcpRoot = Join-Path $faceRoot "parts_mcp_server"
$webPython = Join-Path $webRoot ".venv\Scripts\python.exe"
$mcpPython = Join-Path $mcpRoot ".venv\Scripts\python.exe"
$mcpStatusFile = Join-Path $mcpRoot "logs\mcp_status.json"

if (-not (Test-Path -LiteralPath $webPython -PathType Leaf)) {
    throw "Website environment is missing. Run unified_web_app\install.bat first."
}
if (-not (Test-Path -LiteralPath $mcpPython -PathType Leaf)) {
    throw "MCP environment is missing. Run parts_mcp_server\install.bat first."
}
if (-not (Test-Path -LiteralPath (Join-Path $mcpRoot ".env") -PathType Leaf)) {
    throw "MCP configuration is missing. Create parts_mcp_server\.env first."
}

$webListener = Get-NetTCPConnection -State Listen -LocalPort 5050 -ErrorAction SilentlyContinue
if ($webListener) {
    Write-Host "Website is already running on http://127.0.0.1:5050"
} else {
    $webProcess = Start-Process `
        -FilePath $webPython `
        -ArgumentList @("app.py") `
        -WorkingDirectory $webRoot `
        -WindowStyle Hidden `
        -PassThru
    $webDeadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 500
        $webListener = Get-NetTCPConnection -State Listen -LocalPort 5050 -ErrorAction SilentlyContinue
    } while (-not $webListener -and (Get-Date) -lt $webDeadline)
    if (-not $webListener) {
        throw "Website process did not start successfully (PID $($webProcess.Id))."
    }
    Write-Host "Website started: http://127.0.0.1:5050"
}

$mcpProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "python.exe" -and
    $_.CommandLine -like "*parts_mcp_server*" -and
    $_.CommandLine -like "*xiaozhi_bridge.py*"
}

$monitorReady = $false
if ($mcpProcesses -and (Test-Path -LiteralPath $mcpStatusFile -PathType Leaf)) {
    try {
        $mcpStatus = Get-Content -LiteralPath $mcpStatusFile -Raw | ConvertFrom-Json
        $statusUpdated = [DateTimeOffset]::Parse($mcpStatus.updated_at)
        $statusAge = ([DateTimeOffset]::Now - $statusUpdated).TotalSeconds
        $mcpProcessIds = @($mcpProcesses | Select-Object -ExpandProperty ProcessId)
        $monitorReady = ($mcpProcessIds -contains [int]$mcpStatus.bridge_pid) -and
            ($statusAge -lt 20) -and
            ($mcpStatus.state -ne "stopped")
    } catch {
        $monitorReady = $false
    }
}

if ($mcpProcesses -and -not $monitorReady) {
    Write-Host "Restarting the existing MCP bridge to enable live monitoring..."
    $mcpProcessIds = @($mcpProcesses | Select-Object -ExpandProperty ProcessId)
    $mcpRoots = @($mcpProcesses | Where-Object {
        $mcpProcessIds -notcontains $_.ParentProcessId
    })
    foreach ($mcpRootProcess in $mcpRoots) {
        & taskkill.exe /PID $mcpRootProcess.ProcessId /T /F 2>$null | Out-Null
    }
    Start-Sleep -Seconds 1
    $mcpProcesses = $null
}

if ($mcpProcesses) {
    Write-Host "Xiaozhi MCP bridge is already running."
} else {
    $mcpProcess = Start-Process `
        -FilePath $mcpPython `
        -ArgumentList @("-X", "utf8", "xiaozhi_bridge.py") `
        -WorkingDirectory $mcpRoot `
        -WindowStyle Hidden `
        -PassThru
    Start-Sleep -Seconds 2
    if ($mcpProcess.HasExited) {
        throw "MCP bridge did not start successfully."
    }
    Write-Host "Xiaozhi MCP bridge started."
}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:5050/health" -TimeoutSec 5
if ($health.status -ne "ok") {
    throw "Website health check failed."
}

Write-Host "All services are healthy."
