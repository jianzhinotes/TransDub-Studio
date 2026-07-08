# Post-install bootstrap for the Windows graphical installer.
# Installs uv, provisions ffmpeg, and runs `uv sync` inside the install dir.
# Invoked by installer.iss [Run] as:
#   powershell -ExecutionPolicy Bypass -NoProfile -File bootstrap.ps1 -InstallDir "<app>"
param([string]$InstallDir = $PSScriptRoot)
$ErrorActionPreference = 'Stop'

$log = Join-Path $InstallDir 'install-log.txt'
function Log($m) {
    $line = "$((Get-Date).ToString('s'))  $m"
    Write-Host $line
    Add-Content -Path $log -Value $line
}

Write-Host '========================================================'
Write-Host '  TransDub Studio - first-time setup'
Write-Host '  This downloads a few GB (PyTorch + models). Please wait.'
Write-Host '========================================================'
Log "Bootstrap start in $InstallDir"

# 1) ensure uv --------------------------------------------------------------
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $uv) -and -not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Log 'Installing uv...'
    powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
}
if (-not (Test-Path $uv)) {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $uv = $cmd.Source } else { throw 'uv install failed; see install-log.txt' }
}
Log "uv: $uv"

# 2) provision ffmpeg (repo ships only .exe names, gitignored) ---------------
$ffdir = Join-Path $InstallDir 'ffmpeg'
if (-not (Test-Path (Join-Path $ffdir 'ffmpeg.exe'))) {
    try {
        Log 'Downloading ffmpeg...'
        New-Item -ItemType Directory -Force -Path $ffdir | Out-Null
        $zip = Join-Path $env:TEMP 'ffmpeg.zip'
        $url = 'https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip'
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        $tmp = Join-Path $env:TEMP 'ffmpeg-extract'
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        Get-ChildItem -Path $tmp -Recurse -Include 'ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe' |
            ForEach-Object { Copy-Item $_.FullName -Destination $ffdir -Force }
        Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Log 'ffmpeg ready.'
    } catch {
        Log "WARN: ffmpeg download failed ($_). Install ffmpeg manually into $ffdir if needed."
    }
}

# 3) uv sync ----------------------------------------------------------------
Set-Location $InstallDir
Log 'Running uv sync (this is the long part)...'
& $uv sync 2>&1 | ForEach-Object { Write-Host $_; Add-Content -Path $log -Value $_ }
if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE); see install-log.txt" }

Log 'Setup complete.'
Write-Host ''
Write-Host 'Setup complete. You can launch TransDub Studio from the Start Menu.'
