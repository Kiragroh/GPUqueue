param([string]$Executable = (Join-Path $PSScriptRoot '..\build\GPUqueue.exe'))
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$queueAssembly = [Reflection.Assembly]::LoadFrom((Resolve-Path -LiteralPath $Executable).Path)
$queueFlags = [Reflection.BindingFlags]'Public,NonPublic,Static,Instance'
$queueResults = [Collections.Generic.List[string]]::new()
function Assert-Queue([bool]$Condition,[string]$Name) {if(-not $Condition){throw "FAILED: $Name"}; $queueResults.Add($Name)}
$queueSampler = $queueAssembly.GetType('GPUqueueDesktop.CpuSampler')
$queueCalc = $queueSampler.GetMethod('Calculate',$queueFlags)
Assert-Queue ($queueCalc.Invoke($null,@([long]25,[long]100)) -eq 75) 'CPU delta calculation'
Assert-Queue ($null -eq $queueCalc.Invoke($null,@([long]0,[long]0))) 'CPU zero interval unknown'
Assert-Queue ($null -eq $queueCalc.Invoke($null,@([long]120,[long]100))) 'CPU invalid sample unknown'
$queueData = $queueAssembly.GetType('GPUqueueDesktop.Data')
$queueParse = $queueData.GetMethod('Parse',$queueFlags)
$queueFormType = $queueAssembly.GetType('GPUqueueDesktop.QueueForm')
$queueSettingsType = $queueAssembly.GetType('GPUqueueDesktop.Settings')
$queueSnapshotType = $queueAssembly.GetType('GPUqueueDesktop.Snapshot')
$queueSettings = [Activator]::CreateInstance($queueSettingsType,$true)
$queueCtor = $queueFormType.GetConstructors($queueFlags)[0]
$queueForm = $queueCtor.Invoke(@($queueSettings,$true,$null))
function Field-Queue([string]$Name) {return $queueFormType.GetField($Name,$queueFlags).GetValue($queueForm)}
function Apply-Queue([string]$State,[bool]$Healthy=$true,[bool]$Stale=$false,[string]$CpuState='') {
    $queueSnap = [Activator]::CreateInstance($queueSnapshotType,$true)
    $queueNow = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $queueObserved = if($Stale){$queueNow-30}else{$queueNow}
    $queueJob = if($State -eq 'idle'){''}else{'{"lane":"gpu","state":"'+$State+'","owner":"synthetic","model":"test","run_seconds":30}'}
    if($CpuState){if($queueJob){$queueJob+=','};$queueJob+='{"lane":"cpu","state":"'+$CpuState+'","owner":"CPU synthetic","model":"embed test","run_seconds":30}'}
    $queueJson = '{"paused":false,"gpu":{"name":"Test GPU","used_mb":4096,"free_mb":12288,"total_mb":16384,"utilization":30,"observed_at":'+$queueObserved+'},"jobs":['+$queueJob+']}'
    if($Healthy){$queueSnapshotType.GetField('Health',$queueFlags).SetValue($queueSnap,$queueParse.Invoke($null,@('{"service":"local-gpu-coordinator","worker_alive":true}')))}
    $queueSnapshotType.GetField('Status',$queueFlags).SetValue($queueSnap,$queueParse.Invoke($null,@($queueJson)))
    $queueSnapshotType.GetField('CpuPercent',$queueFlags).SetValue($queueSnap,[double]42)
    $queueFormType.GetMethod('Apply',$queueFlags).Invoke($queueForm,@($queueSnap)) | Out-Null
}
try {
    $queueForm.Show(); [Windows.Forms.Application]::DoEvents()
    Apply-Queue 'idle'
    Assert-Queue ((Field-Queue 'visualState') -eq 'online') 'Idle green'
    Assert-Queue ((Field-Queue 'menuCpu').Text -eq 'CPU gesamt: 42 %') 'CPU in context menu'
    Assert-Queue ((Field-Queue 'menuGpu').Text -like 'GPU gesamt: 30 %*') 'GPU and VRAM in context menu'
    $queueIcon=(Field-Queue 'statusIcons')['online']
    Apply-Queue 'running'
    Assert-Queue ((Field-Queue 'visualState') -eq 'busy') 'Running amber'
    Assert-Queue ((Field-Queue 'menuStatus').Text -like 'Läuft*') 'Running status text'
    Assert-Queue ((Field-Queue 'menuJobs').Text -like 'GPU-Queue: 1 aktiv*') 'Active count in context menu'
    Assert-Queue ($queueIcon.Handle -ne [IntPtr]::Zero) 'Previous icon stays valid after state change'
    Apply-Queue 'queued'
    Assert-Queue ((Field-Queue 'visualState') -eq 'waiting') 'Queued amber'
    Apply-Queue 'recovery_blocked'
    Assert-Queue ((Field-Queue 'visualState') -eq 'blocked') 'Recovery red'
    Assert-Queue ((Field-Queue 'laneTabs').SelectedIndex -eq 0 -and (Field-Queue 'laneTabs').TabPages[0].Text -eq 'GPU') 'GPU tab selected by default'
    Apply-Queue 'running' $true $false 'recovery_blocked'
    Assert-Queue ((Field-Queue 'visualState') -eq 'busy' -and (Field-Queue 'headline').Text -like '*GPU-Aufträge*') 'GPU running remains amber with CPU blocked'
    Assert-Queue ((Field-Queue 'jobs').Rows.Count -eq 1 -and (Field-Queue 'jobs').Rows[0].Cells[0].Value -eq 'GPU') 'GPU tab excludes CPU jobs'
    Assert-Queue ((Field-Queue 'cpuJobs').Rows.Count -eq 1 -and (Field-Queue 'cpuJobs').Rows[0].Cells[0].Value -eq 'CPU') 'CPU jobs in separate grid'
    Assert-Queue ((Field-Queue 'cpuCount').Text -like '*1 blockiert*' -and (Field-Queue 'menuJobs').Text -like '*0 blockiert') 'CPU blockade visible only in CPU counters'
    (Field-Queue 'laneTabs').SelectedIndex=1
    Apply-Queue 'idle' $true $false 'recovery_blocked'
    Assert-Queue ((Field-Queue 'visualState') -eq 'online') 'GPU ready stays green with CPU blocked'
    Assert-Queue ((Field-Queue 'laneTabs').SelectedIndex -eq 1) 'Polling preserves selected CPU tab'
    Apply-Queue 'idle' $true $false 'running'
    Assert-Queue ((Field-Queue 'visualState') -eq 'online') 'CPU running does not mark GPU busy'
    Apply-Queue 'idle' $true $false 'queued'
    Assert-Queue ((Field-Queue 'visualState') -eq 'online') 'CPU waiting does not mark GPU waiting'
    Apply-Queue 'idle' $true $true
    Assert-Queue ((Field-Queue 'menuGpu').Text -eq 'GPU gesamt: keine aktuelle Messung') 'Stale GPU not presented as current'
    Apply-Queue 'idle' $false
    Assert-Queue ((Field-Queue 'visualState') -eq 'offline') 'Offline red'
    Assert-Queue ((Field-Queue 'menuJobs').Text -eq 'GPU-Queue: Status unbekannt') 'Offline clears queue counters'
    Assert-Queue ((Field-Queue 'cpuJobs').Rows.Count -eq 0 -and (Field-Queue 'cpuCount').Text -like '*unbekannt*') 'Offline clears CPU tab too'
    Assert-Queue ($queueForm.ShowIcon -and $queueForm.ShowInTaskbar) 'Window requests taskbar icon'
    $queueEmbeddedIcon=[Drawing.Icon]::ExtractAssociatedIcon((Resolve-Path $Executable).Path)
    Assert-Queue ($null -ne $queueEmbeddedIcon) 'Embedded executable icon readable'
    $queueEmbeddedIcon.Dispose()
} finally {$queueForm.Dispose()}
[pscustomobject]@{Passed=$queueResults.Count;Checks=$queueResults;GpuInference=$false} | ConvertTo-Json -Depth 4
