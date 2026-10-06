param([Parameter(Mandatory=$true)][string]$PythonPath)
$ErrorActionPreference = 'Stop'
& $PythonPath (Join-Path $PSScriptRoot 'client.py') open --control
if($LASTEXITCODE -ne 0){throw 'Could not open the scoped operator session.'}
