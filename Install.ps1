param([Parameter(Mandatory=$true)][string]$PythonPath, [switch]$SupervisorOnly)
$ErrorActionPreference = 'Stop'
$queuePython = (Resolve-Path -LiteralPath $PythonPath).Path
$queuePythonW = Join-Path (Split-Path $queuePython) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $queuePythonW)) {throw 'pythonw.exe must be beside the supplied Python interpreter.'}
& $queuePython -c 'import requests, psutil, win32crypt, win32event, win32job'
if ($LASTEXITCODE -ne 0) {throw 'Install requirements.txt into this interpreter first.'}
$queueInstall = Join-Path $env:LOCALAPPDATA 'Programs\GPUqueue'
$queueState = Join-Path $env:LOCALAPPDATA 'GPUqueue\state'
# First-install helper. Do not silently replace an existing local deployment.
if (Test-Path -LiteralPath (Join-Path $queueInstall 'config.json')) {throw 'An installation already exists. See docs/operations.md for updates.'}
if (Get-ScheduledTask -TaskName 'GPUqueue Tray','GPUqueue Supervisor' -ErrorAction SilentlyContinue) {throw 'GPUqueue tasks already exist; no changes made.'}
if (Get-NetTCPConnection -LocalPort 11436 -State Listen -ErrorAction SilentlyContinue) {throw 'Port 11436 is in use; do not start a competing coordinator.'}
if (-not $SupervisorOnly) {& (Join-Path $PSScriptRoot 'Build.ps1')}
New-Item -ItemType Directory -Path $queueInstall,$queueState,(Join-Path $queueInstall 'coordinator') -Force | Out-Null
foreach($queueFile in Get-ChildItem (Join-Path $PSScriptRoot 'coordinator') -File) {
    if($queueFile.Extension -in '.py','.html','.ps1') {Copy-Item -LiteralPath $queueFile.FullName -Destination (Join-Path $queueInstall 'coordinator')}
}
foreach($queueFile in 'supervisor.py','Start-GPUqueue.ps1') {Copy-Item -LiteralPath (Join-Path $PSScriptRoot $queueFile) -Destination $queueInstall}
if(-not $SupervisorOnly){Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'build\GPUqueue.exe') -Destination $queueInstall}
@{python=$queuePython;coordinator_root=(Join-Path $queueInstall 'coordinator');state_dir=$queueState;url='http://127.0.0.1:11436';legacy_roots=@();desktop_enabled=(-not $SupervisorOnly)} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $queueInstall 'config.json') -Encoding UTF8
$queueUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$queuePrincipal = New-ScheduledTaskPrincipal -UserId $queueUser -LogonType Interactive -RunLevel Limited
$queueSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$queueLogin = New-ScheduledTaskTrigger -AtLogOn -User $queueUser
$queueRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$queueChanged = @()
try {
    $queueAction = New-ScheduledTaskAction -Execute $queuePythonW -Argument ('"{0}" --config "{1}"' -f (Join-Path $queueInstall 'supervisor.py'),(Join-Path $queueInstall 'config.json')) -WorkingDirectory $queueInstall
    Register-ScheduledTask -TaskName 'GPUqueue Supervisor' -Action $queueAction -Trigger @($queueLogin,$queueRepeat) -Settings $queueSettings -Principal $queuePrincipal -Description 'GPUqueue loopback coordinator watchdog' | Out-Null
    $queueChanged += 'GPUqueue Supervisor'
    if(-not $SupervisorOnly) {
        $queueAction = New-ScheduledTaskAction -Execute (Join-Path $queueInstall 'GPUqueue.exe') -Argument '--tray' -WorkingDirectory $queueInstall
        Register-ScheduledTask -TaskName 'GPUqueue Tray' -Action $queueAction -Trigger $queueLogin -Settings $queueSettings -Principal $queuePrincipal -Description 'GPUqueue local status tray' | Out-Null
        $queueChanged += 'GPUqueue Tray'
    }
} catch {
    foreach($queueTask in $queueChanged){Unregister-ScheduledTask -TaskName $queueTask -Confirm:$false -ErrorAction SilentlyContinue}
    throw
}
Start-ScheduledTask -TaskName 'GPUqueue Supervisor'
if(-not $SupervisorOnly){Start-ScheduledTask -TaskName 'GPUqueue Tray'}
[pscustomobject]@{Installed=$queueInstall;State=$queueState;Autostart='Current user logon';Port=11436}
