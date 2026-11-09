param(
    [string]$Dump,
    [string]$Scenario,
    [string]$TargetUid,
    [string]$Port,
    [string]$Client,
    [string]$DataDir,
    [ValidateSet('usb','blueshark')][string]$Transport,
    [switch]$Execute
)
$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $pythonExe)) { throw 'Сначала подготовьте окружение через start.ps1.' }
if ((!$Dump -and !$Scenario) -or ($Dump -and $Scenario)) { throw 'Укажите один параметр: -Dump или -Scenario.' }
if ($Dump) { $Dump = (Resolve-Path -LiteralPath $Dump).ProviderPath }
if ($Scenario) { $Scenario = (Resolve-Path -LiteralPath $Scenario).ProviderPath }
if ($Client) { $Client = (Resolve-Path -LiteralPath $Client).ProviderPath }
if ($DataDir) { $DataDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DataDir) }
$forwardedArgs = @('-m','pm3gui.autowrite')
if ($Dump) { $forwardedArgs += @('--dump',$Dump) }
if ($Scenario) { $forwardedArgs += @('--scenario',$Scenario) }
if ($TargetUid) { $forwardedArgs += @('--target-uid',$TargetUid) }
if ($Port) { $forwardedArgs += @('--port',$Port) }
if ($Client) { $forwardedArgs += @('--client',$Client) }
if ($DataDir) { $forwardedArgs += @('--data-dir',$DataDir) }
if ($Transport) { $forwardedArgs += @('--transport',$Transport) }
if ($Execute) { $forwardedArgs += '--execute' }
Push-Location -LiteralPath $PSScriptRoot
try { & $pythonExe @forwardedArgs; $scriptExitCode = $LASTEXITCODE }
finally { Pop-Location }
exit $scriptExitCode
