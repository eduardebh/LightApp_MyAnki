param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $Here ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "[py] ERROR: .venv not found at $VenvPython" -ForegroundColor Red
    Write-Host "[py] Create it and install requirements first." -ForegroundColor Yellow
    exit 1
}

& $VenvPython @Args
exit $LASTEXITCODE
