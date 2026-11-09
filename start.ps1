param([switch]$Demo)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskCandidates = @(
        'py',
        'python'
    )
    $taskBootstrap = $null
    foreach ($taskCandidate in $taskCandidates) {
        if (Get-Command $taskCandidate -ErrorAction SilentlyContinue) {
            & $taskCandidate -c 'import sys; assert sys.version_info >= (3, 12)' 2>$null
            if ($LASTEXITCODE -eq 0) { $taskBootstrap = $taskCandidate; break }
        }
    }
    if (-not $taskBootstrap) { throw 'Install Python 3.12 or newer, then run start.ps1 again.' }
    & $taskBootstrap -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the Python environment.' }
}
& $taskPython -c 'import PySide6, serial' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt') --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install dependencies.' }
}
if ($Demo) { & $taskPython (Join-Path $PSScriptRoot 'run_gui.py') --demo }
else { & $taskPython (Join-Path $PSScriptRoot 'run_gui.py') }
