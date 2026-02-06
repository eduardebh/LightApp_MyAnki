<#
Run LightApp using the local .venv and keep mytools up to date.
Usage:
  .\run_lightapp.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$OriginalDir = Get-Location

$VenvPython = Join-Path $Here '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    Write-Host "[run_lightapp] ERROR: .venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "[run_lightapp] Create it and install requirements first." -ForegroundColor Yellow
    exit 1
}

try {
    Push-Location $Here

    # Update helper repo + editable install (best-effort)
    try {
        Write-Host "[run_lightapp] Updating mytools..."
        & (Join-Path $Here 'update_mytools.ps1') | Out-Host
    } catch {
        Write-Host "[run_lightapp] NOTE: update_mytools.ps1 failed (continuing): $_" -ForegroundColor Yellow
    }

    # Open the browser once the dev server is accepting connections.
    # Use a separate helper process (NOT Start-Job) so we don't leave background jobs running
    # in other terminals/sessions that can cause double-opens.
    $url = 'http://127.0.0.1:5000'
    $alreadyListening = $false
    try { $alreadyListening = (Test-NetConnection -ComputerName '127.0.0.1' -Port 5000 -InformationLevel Quiet) } catch { $alreadyListening = $false }
    if (-not $alreadyListening) {
        $helper = Join-Path $Here 'open_lightapp_browser.ps1'
        if (Test-Path $helper) {
            Start-Process -WindowStyle Hidden -FilePath 'powershell' -ArgumentList @(
                '-NoProfile',
                '-ExecutionPolicy', 'Bypass',
                '-File', $helper,
                '-Url', $url,
                '-HostName', '127.0.0.1',
                '-Port', '5000',
                '-TimeoutSeconds', '20',
                '-LockWindowSeconds', '300'
            ) | Out-Null
        }
    } else {
        Write-Host "[run_lightapp] NOTE: Server already listening on 127.0.0.1:5000; not opening browser again." -ForegroundColor Yellow
    }

    Write-Host "[run_lightapp] Starting LightApp with .venv Python: $VenvPython"
    & $VenvPython (Join-Path $Here 'app_light.py')
} finally {
    try { Pop-Location } catch { }
    try { Set-Location $OriginalDir } catch { }
}
