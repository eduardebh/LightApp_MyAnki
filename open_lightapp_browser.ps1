param(
    [string]$Url = 'http://127.0.0.1:5000',
    [string]$HostName = '127.0.0.1',
    [int]$Port = 5000,
    [int]$TimeoutSeconds = 20,
    [string]$LockPath = "",
    [int]$LockWindowSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'SilentlyContinue'

if (-not $LockPath) {
    $LockPath = Join-Path $env:TEMP "LightApp_opened_${HostName}_${Port}.lock"
}

$mutexName = "Global\\LightAppOpenBrowser_${HostName}_${Port}"
$mutex = $null
try {
    $mutex = New-Object System.Threading.Mutex($false, $mutexName)
    # If we can't acquire quickly, assume another opener is running.
    if (-not $mutex.WaitOne(0)) { exit 0 }
} catch {
    # If mutex creation fails, we still rely on the lock file below.
    $mutex = $null
}

try {
    # 1) Wait for the port to accept connections.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            if (Test-NetConnection -ComputerName $HostName -Port $Port -InformationLevel Quiet) { break }
        } catch { }
        Start-Sleep -Milliseconds 250
    }

    # 2) Wait for the HTTP endpoint to respond (avoids opening a blank/failed tab during Flask reloader churn).
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($resp -and $resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500) { break }
        } catch { }
        Start-Sleep -Milliseconds 250
    }

    # 3) Lock window check right before opening.
    try {
        if (Test-Path $LockPath) {
            $age = (Get-Date) - (Get-Item $LockPath).LastWriteTime
            if ($age.TotalSeconds -lt $LockWindowSeconds) { exit 0 }
        }
    } catch { }

    try { Set-Content -Path $LockPath -Value (Get-Date).ToString('o') -Encoding ascii -Force } catch { }
    try { Start-Process $Url } catch { }
} finally {
    try { if ($mutex) { $mutex.ReleaseMutex() } } catch { }
    try { if ($mutex) { $mutex.Dispose() } } catch { }
}
