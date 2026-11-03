$ErrorActionPreference = "Stop"

$faceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$webRoot = Join-Path $faceRoot "unified_web_app"
$mcpRoot = Join-Path $faceRoot "parts_mcp_server"

function Test-CommandContains {
    param(
        [AllowNull()][string]$CommandLine,
        [string]$Text
    )
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    return $CommandLine.IndexOf($Text, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

$allProcesses = Get-CimInstance Win32_Process
$targets = @($allProcesses | Where-Object {
    $isPython = $_.Name -in @("python.exe", "pythonw.exe")
    $isWebsite = (Test-CommandContains $_.CommandLine $webRoot) -and
        (Test-CommandContains $_.CommandLine "app.py")
    $isMcp = (Test-CommandContains $_.CommandLine $mcpRoot) -and
        ((Test-CommandContains $_.CommandLine "xiaozhi_bridge.py") -or
         (Test-CommandContains $_.CommandLine "parts_mcp.py"))
    $isPython -and ($isWebsite -or $isMcp)
})

if (-not $targets) {
    Write-Host "Website and MCP processes are already stopped."
    exit 0
}

$targetIds = @($targets | Select-Object -ExpandProperty ProcessId)
$roots = @($targets | Where-Object { $targetIds -notcontains $_.ParentProcessId })

foreach ($root in $roots) {
    & taskkill.exe /PID $root.ProcessId /T /F 2>$null | Out-Null
}

$deadline = (Get-Date).AddSeconds(5)
do {
    Start-Sleep -Milliseconds 250
    $remaining = @(Get-CimInstance Win32_Process | Where-Object {
        $targetIds -contains $_.ProcessId
    })
} while ($remaining -and (Get-Date) -lt $deadline)

if ($remaining) {
    $remainingIds = ($remaining | Select-Object -ExpandProperty ProcessId) -join ", "
    Write-Error "Could not stop project processes: $remainingIds"
    exit 1
}

$listener = Get-NetTCPConnection -State Listen -LocalPort 5050 -ErrorAction SilentlyContinue
if ($listener) {
    Write-Error "Project processes stopped, but port 5050 is still used by another process."
    exit 1
}

Write-Host "Website and MCP processes stopped successfully."
exit 0
