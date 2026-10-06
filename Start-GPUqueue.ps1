param([switch]$Headless)
$ErrorActionPreference = 'Stop'
Start-ScheduledTask -TaskName 'GPUqueue Supervisor'
if(-not $Headless){Start-ScheduledTask -TaskName 'GPUqueue Tray'}
