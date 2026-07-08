# TransDub Studio one-command installer (Windows / PowerShell).
#
#   powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/jianzhinotes/TransDub-Studio/main/install.ps1 | iex"
#
# Installs uv, clones the repo, and syncs dependencies. $env:TDS_DIR overrides the install dir.
$ErrorActionPreference = 'Stop'

$Repo = 'https://github.com/jianzhinotes/TransDub-Studio.git'
$Dest = if ($env:TDS_DIR) { $env:TDS_DIR } else { Join-Path $HOME 'TransDub-Studio' }

Write-Host '==> TransDub Studio installer' -ForegroundColor Cyan

# 1) uv (Python toolchain manager)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host '==> Installing uv...'
    powershell -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$HOME\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv not found on PATH. Open a new terminal and re-run this script.'
}

# 2) git check
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'git is required. Install Git for Windows from https://git-scm.com/download/win and re-run.'
}

# 3) clone or update
if (Test-Path (Join-Path $Dest '.git')) {
    Write-Host "==> Updating existing install at $Dest"
    git -C $Dest pull --ff-only
} else {
    Write-Host "==> Cloning into $Dest"
    git clone $Repo $Dest
}

# 4) dependencies (torch etc. - several GB; GPU CUDA build on Windows)
Set-Location $Dest
Write-Host '==> Installing dependencies (downloads several GB, please be patient)...'
uv sync

Write-Host ''
Write-Host 'Installation complete.' -ForegroundColor Green
Write-Host ''
Write-Host '   Launch it with:'
Write-Host "       cd `"$Dest`"; uv run python sp.py"
Write-Host ''
Write-Host '   (First launch downloads the recognition model on demand; after that it runs local.)'
