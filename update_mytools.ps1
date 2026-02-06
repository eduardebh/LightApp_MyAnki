<#
PowerShell helper script to update `mytools` from its remote repository.

# NOTE: This script NEVER launches the app nor opens the browser.
# It is safe to run standalone; it only updates code and the editable install.
# To launch the app and open the browser, use run_lightapp.ps1.

This workspace can be opened either at:
    - the app root (where `mytools/` lives), OR
    - a parent repo that contains `LightApp/mytools/`.

Goal:
- Always update the `mytools` repo to the latest commit on its default branch
    (detected from origin/HEAD, fallback 'main').
- Do NOT create commits or push anything.

Usage:
    .\update_mytools.ps1

Notes:
- If `mytools` is a git submodule, the parent repo may become dirty (gitlink updated).
    Commit that change when you actually want to bump the pinned submodule SHA.
- If a local venv exists at `.venv`, the script best-effort runs `pip install -e ./mytools`
    so the app uses the updated code.
#>

param(
    # Force a specific branch to pull in the submodule (e.g. 'main', 'develop').
    # If omitted, the script detects origin's HEAD branch (fallback 'main').
    [string]$Branch = '',

    # If set, do NOT discard local changes inside the submodule.
    # Default behaviour is to hard reset + clean so the submodule always matches the remote.
    [switch]$KeepLocalChanges
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$OriginalDir = Get-Location
function Exit-UpdateMytools {
    param([int]$Code)
    try { Set-Location $OriginalDir } catch { }
    exit $Code
}

# Determine repo root (git) and app root (this script's folder)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

$RepoRoot = ''
try {
    $RepoRoot = (git rev-parse --show-toplevel 2>$null).Trim()
} catch {
    $RepoRoot = $ScriptDir
}
if (-not $RepoRoot) { $RepoRoot = $ScriptDir }

# Normalize git-style paths (often forward slashes) to a Windows full path so
# relative label computation is stable.
try {
    $RepoRoot = [System.IO.Path]::GetFullPath(($RepoRoot -replace '/', '\\'))
} catch {
    # Best-effort only.
}
Set-Location $RepoRoot

Write-Host "[update_mytools] Repo root: $RepoRoot"

$AppRoot = $ScriptDir
Write-Host "[update_mytools] App root:  $AppRoot"

function Resolve-MytoolsPath {
    param(
        [string]$RepoRoot,
        [string]$ScriptDir
    )
    $candidates = @(
        (Join-Path $ScriptDir 'mytools'),
        (Join-Path $RepoRoot 'mytools'),
        (Join-Path $RepoRoot 'LightApp\mytools'),
        (Join-Path $ScriptDir 'LightApp\mytools')
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

function Get-MytoolsRelativeLabel {
    param(
        [string]$FullPath,
        [string]$RepoRoot,
        [string]$ScriptDir
    )
    # Prefer a true repo-relative path when possible (works well on Windows PowerShell 5.1).
    try {
        if ($RepoRoot -and (Test-Path -LiteralPath $RepoRoot) -and $FullPath -and (Test-Path -LiteralPath $FullPath)) {
            Push-Location $RepoRoot
            $rel = (Resolve-Path -LiteralPath $FullPath -Relative | Select-Object -First 1)
            Pop-Location

            if ($rel) {
                $s = $rel.ToString()
                $s = $s -replace '^[.][\\/]', ''
                $s = $s.TrimStart([char]92, [char]47)
                return ($s -replace '/', '\\')
            }
        }
    } catch {
        try { Pop-Location } catch { }
    }

    # Fallback: absolute full path.
    try { return [System.IO.Path]::GetFullPath($FullPath) } catch { return $FullPath }
}

function Is-GitRepo {
    param([string]$Path)
    try {
        git -C $Path rev-parse --is-inside-work-tree 1>$null 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Is-SubmodulePath {
    param(
        [string]$RepoRoot,
        [string]$SubPathLabel
    )
    try {
        $gm = Join-Path $RepoRoot '.gitmodules'
        if (-not (Test-Path $gm)) { return $false }
        $txt = Get-Content $gm -Raw
        # .gitmodules paths are written with forward slashes
        $norm = $SubPathLabel.Replace('\\','/').Trim()
        return ($txt -match ([regex]::Escape("path = $norm")))
    } catch {
        return $false
    }
}

function Warn-If-DirtyWorkingTree {
    $porcelain = git status --porcelain
    if ($porcelain) {
        Write-Host "[update_mytools] NOTE: Working tree is not clean; continuing anyway." -ForegroundColor Yellow
    }
}

Warn-If-DirtyWorkingTree

# 1) Resolve mytools path (supports both repo layouts)
$mytoolsFull = Resolve-MytoolsPath -RepoRoot $RepoRoot -ScriptDir $ScriptDir
if (-not $mytoolsFull) {
    Write-Host "[ERROR] Could not find 'mytools' folder (tried ./mytools and ./LightApp/mytools)." -ForegroundColor Red
    Exit-UpdateMytools 1
}

$mytoolsLabel = Get-MytoolsRelativeLabel -FullPath $mytoolsFull -RepoRoot $RepoRoot -ScriptDir $ScriptDir
$mytoolsDisplay = $mytoolsLabel
if ($mytoolsDisplay -and ($mytoolsDisplay -notmatch '^[A-Za-z]:\\')) {
    $mytoolsDisplay = ".\\$mytoolsDisplay"
} elseif (-not $mytoolsDisplay) {
    $mytoolsDisplay = '.'
}
Write-Host "[update_mytools] mytools path: $mytoolsDisplay"

if (-not (Is-GitRepo -Path $mytoolsFull)) {
    Write-Host "[ERROR] mytools path exists but is not a git repo: $mytoolsFull" -ForegroundColor Red
    Exit-UpdateMytools 1
}

# If this repo uses submodules and mytools is a submodule, best-effort init it.
$isSubmodule = Is-SubmodulePath -RepoRoot $RepoRoot -SubPathLabel $mytoolsLabel
if ($isSubmodule) {
    Write-Host "[update_mytools] Detected mytools as a git submodule; ensuring it's initialized..."
    git submodule update --init --recursive -- $mytoolsLabel
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] git submodule update failed for $mytoolsLabel" -ForegroundColor Red
        Exit-UpdateMytools 1
    }
}

# 2) Update mytools repo
Write-Host "[update_mytools] Updating mytools '$mytoolsLabel'..."

# If the submodule is dirty, discard local changes so we can always align with remote.
$subDirty = git -C $mytoolsFull status --porcelain
if ($subDirty) {
    if ($KeepLocalChanges) {
        Write-Host "[update_mytools] NOTE: mytools has local changes; keeping them due to -KeepLocalChanges." -ForegroundColor Yellow
    } else {
        Write-Host "[update_mytools] WARNING: mytools has local changes; discarding to match remote." -ForegroundColor Yellow
        git -C $mytoolsFull reset --hard 2>&1 | Out-String
        git -C $mytoolsFull clean -fd 2>&1 | Out-String
    }
}

Push-Location $mytoolsFull

# Temporarily relax error action so stderr from git doesn't spam PowerShell errors.
# We still validate every command using $LASTEXITCODE.
$oldEAP = $ErrorActionPreference
$ErrorActionPreference = 'SilentlyContinue'

# Detect remote HEAD branch (fallback to 'main'), unless a branch is forced via -Branch.
$defaultBranch = 'main'
if ($Branch) {
    $defaultBranch = $Branch.Trim()
} else {
    try {
        # Most reliable: origin/HEAD symbolic ref
        $sym = git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>$null | Out-String
        $sym = $sym.Trim()
        if ($sym -match '^origin\/(.+)$') {
            $defaultBranch = $Matches[1]
        } else {
            $remoteInfo = git remote show origin 2>$null | Out-String
            if ($remoteInfo -match 'HEAD branch: (\S+)') { $defaultBranch = $Matches[1] }
        }
    } catch { }
}

Write-Host "[update_mytools] mytools default branch: $defaultBranch"

# Print the exact origin URL (helps detect updating the wrong remote).
try {
    $originUrl = (git remote get-url origin 2>$null | Out-String).Trim()
    if ($originUrl) { Write-Host "[update_mytools] mytools origin: $originUrl" }
} catch { }

# Fetch updates
git fetch origin --prune
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] git fetch failed in submodule" -ForegroundColor Red
    Pop-Location
    $ErrorActionPreference = $oldEAP
    Exit-UpdateMytools 1
}

# Determine current branch inside submodule
$currentSubBranch = git rev-parse --abbrev-ref HEAD 2>$null | Out-String
$currentSubBranch = $currentSubBranch.Trim()
if ($currentSubBranch -ne $defaultBranch) {
    Write-Host "[update_mytools] Checking out $defaultBranch in mytools (was $currentSubBranch)"
    $checkoutOut = git checkout $defaultBranch 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[update_mytools] Local branch $defaultBranch not found; creating tracking branch from origin/$defaultBranch"
        git fetch origin $defaultBranch
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] Failed to fetch origin/$defaultBranch" -ForegroundColor Red
            Pop-Location
            $ErrorActionPreference = $oldEAP
            Exit-UpdateMytools 1
        }
        git checkout -b $defaultBranch origin/$defaultBranch 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] Failed to create tracking branch $defaultBranch" -ForegroundColor Red
            Pop-Location
            $ErrorActionPreference = $oldEAP
            Exit-UpdateMytools 1
        }
    } else {
        Write-Host $checkoutOut.Trim()
    }
} else {
    Write-Host "[update_mytools] mytools already on $defaultBranch"
}

# Pull (prefer fast-forward). If the local branch diverged and we're allowed to discard, force-align to remote.
# Always align to the remote tip. A plain pull can silently fail on divergence.
git fetch origin $defaultBranch --prune 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Failed to fetch origin/$defaultBranch" -ForegroundColor Red
    Pop-Location
    $ErrorActionPreference = $oldEAP
    Exit-UpdateMytools 1
}

if (-not $KeepLocalChanges) {
    git reset --hard "origin/$defaultBranch" 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to reset to origin/$defaultBranch" -ForegroundColor Red
        Pop-Location
        $ErrorActionPreference = $oldEAP
        Exit-UpdateMytools 1
    }
    git clean -fd 2>&1 | Out-String
} else {
    # Best-effort fast-forward when keeping local changes.
    git pull --ff-only origin $defaultBranch 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Could not fast-forward while keeping local changes. Resolve manually or re-run without -KeepLocalChanges." -ForegroundColor Red
        Pop-Location
        $ErrorActionPreference = $oldEAP
        Exit-UpdateMytools 1
    }
}

# Final verification: HEAD must match origin/<branch> unless we're keeping local changes.
try {
    $head = (git rev-parse --short HEAD 2>$null | Out-String).Trim()
    $remote = (git rev-parse --short "origin/$defaultBranch" 2>$null | Out-String).Trim()
    if ($head -and $remote -and ($head -ne $remote) -and (-not $KeepLocalChanges)) {
        Write-Host "[ERROR] Verification failed: HEAD=$head != origin/$defaultBranch=$remote" -ForegroundColor Red
        Pop-Location
        $ErrorActionPreference = $oldEAP
        Exit-UpdateMytools 1
    }
    $dirtyNow = git status --porcelain
    if ($dirtyNow -and (-not $KeepLocalChanges)) {
        Write-Host "[ERROR] Verification failed: mytools working tree not clean after reset/clean." -ForegroundColor Red
        Pop-Location
        $ErrorActionPreference = $oldEAP
        Exit-UpdateMytools 1
    }
} catch {
    # Best-effort verification only.
}

$sha = git rev-parse --short HEAD
try {
    $remoteSha = (git rev-parse --short "origin/$defaultBranch" 2>$null | Out-String).Trim()
    if ($remoteSha) {
        Write-Host "[update_mytools] mytools updated to $sha (origin/$defaultBranch = $remoteSha)"
    } else {
        Write-Host "[update_mytools] mytools updated to $sha"
    }
} catch {
    Write-Host "[update_mytools] mytools updated to $sha"
}

# Upstream tracking diagnostics (helps catch detached HEAD / wrong branch / no upstream).
try {
    $headBranch = (git rev-parse --abbrev-ref HEAD 2>$null | Out-String).Trim()
    $upstream = (git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null | Out-String).Trim()
    if ($headBranch) { Write-Host "[update_mytools] mytools HEAD branch: $headBranch" }
    if ($upstream) {
        Write-Host "[update_mytools] mytools upstream: $upstream"
    } else {
        Write-Host "[update_mytools] NOTE: mytools has no upstream configured (ok if you always reset to origin/$defaultBranch)." -ForegroundColor Yellow
    }
} catch { }

# Restore error action
$ErrorActionPreference = $oldEAP
Pop-Location

# 3) Ensure the helper package is installed in the local venv (best-effort)
try {
    $venvPython = Join-Path $AppRoot '.venv\Scripts\python.exe'
    if (Test-Path $venvPython) {
        Write-Host "[update_mytools] Installing/updating editable package: -e $mytoolsLabel (using .venv)"
        & $venvPython -m pip install -e $mytoolsFull | Out-Host

        # Show pip metadata for additional sanity checking.
        try {
            & $venvPython -m pip show frequency-db-utils | Out-Host
        } catch { }

        # Verification: show the runtime signature from the venv interpreter.
        try {
            & $venvPython -c "import sys, frequency_db_utils as f; print('[update_mytools] sys.executable:', sys.executable); print('[update_mytools] frequency_db_utils.__file__:', getattr(f,'__file__',None)); print('[update_mytools] frequency_db_utils.runtime_signature:', f.runtime_signature())" | Out-Host
        } catch {
            Write-Host "[update_mytools] NOTE: Could not import frequency_db_utils in .venv after install (non-fatal): $_" -ForegroundColor Yellow
        }

        # Compare with the system `python` on PATH (common source of 'running old versions').
        try {
            $pathPy = (Get-Command python 2>$null | Select-Object -First 1).Path
            if ($pathPy) {
                Write-Host "[update_mytools] python on PATH: $pathPy"
                & python -c "import sys; print('[update_mytools] PATH python sys.executable:', sys.executable)" 2>$null | Out-Host

                $pathImport = & python -c "import frequency_db_utils as f; print('[update_mytools] PATH python frequency_db_utils.__file__:', getattr(f,'__file__',None)); print('[update_mytools] PATH python frequency_db_utils.runtime_signature:', f.runtime_signature())" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $pathImport | Out-Host
                } else {
                    Write-Host "[update_mytools] NOTE: PATH python could not import frequency_db_utils (this is OK if you run the app from .venv)." -ForegroundColor Yellow
                }
            }
        } catch {
            Write-Host "[update_mytools] NOTE: PATH python could not import frequency_db_utils (this is OK if you run the app from .venv)." -ForegroundColor Yellow
        }
    } else {
        Write-Host "[update_mytools] NOTE: .venv not found; skipping local pip install -e ./mytools"
    }
} catch {
    Write-Host "[update_mytools] NOTE: pip install -e ./mytools failed (non-fatal): $_"
}

if ($isSubmodule) {
    Write-Host "[update_mytools] Done. If the submodule pointer changed, commit '$mytoolsLabel' in the parent repo when ready."
} else {
    Write-Host "[update_mytools] Done."
}

Exit-UpdateMytools 0
