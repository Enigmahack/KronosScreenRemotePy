# Build the Windows bundle from a LOCAL copy (never build over SMB).
#   powershell -File packaging\build_windows.ps1 [-Work C:\build\KronosScreenRemotePy]
# Needs: uv (winget install astral-sh.uv). Output: <Work>\dist\KronosScreenRemote\
param([string]$Work = 'C:\build\KronosScreenRemotePy')
$ErrorActionPreference = 'Stop'
$src = Split-Path $PSScriptRoot -Parent
New-Item -ItemType Directory -Force $Work | Out-Null
robocopy $src $Work /MIR /XD .venv .buildvenv .git __pycache__ build dist librarian_backups local_library tests .claude `
    /XF *.log settings.json cal_data.json *_cache.json dumped_banks.json /NFL /NDL /NJH /NJS /NP | Out-Null
Set-Location $Work
if (-not (Test-Path .buildvenv)) { uv venv --python 3.13 .buildvenv }
uv pip install --python .buildvenv\Scripts\python.exe "PySide6>=6.11" "numpy>=2.0" "sounddevice>=0.5" "soxr>=1.0" pyinstaller
.buildvenv\Scripts\pyinstaller.exe packaging\KronosScreenRemote.spec --noconfirm --distpath dist --workpath build
Write-Host "Built: $Work\dist\KronosScreenRemote\KronosScreenRemote.exe"
