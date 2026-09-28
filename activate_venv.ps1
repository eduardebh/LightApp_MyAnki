<#
Activate the local virtual environment for LightApp.
Usage:
  .\activate_venv.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$activate = Join-Path $Here '.venv\Scripts\Activate.ps1'

if (-not (Test-Path $activate)) {
    Write-Host "[activate_venv] ERROR: .venv not found at $activate" -ForegroundColor Red
    Write-Host "[activate_venv] Create it with: python -m venv .venv" -ForegroundColor Yellow
    exit 1
}

& $activate
Write-Host "[activate_venv] .venv activated."
