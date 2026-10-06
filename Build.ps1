param([string]$OutputDirectory = (Join-Path $PSScriptRoot 'build'))
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The .NET Framework C# compiler is required.' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$output = Join-Path $OutputDirectory 'GPUqueue.exe'
& $compiler /nologo /target:winexe /optimize+ /langversion:5 /platform:anycpu /reference:Microsoft.CSharp.dll /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll /reference:System.Net.Http.dll "/win32manifest:$(Join-Path $PSScriptRoot 'desktop\app.manifest')" "/win32icon:$(Join-Path $PSScriptRoot 'desktop\GPUqueue.ico')" "/out:$output" (Join-Path $PSScriptRoot 'desktop\GPUqueue.cs') (Join-Path $PSScriptRoot 'desktop\DesktopServices.cs')
if ($LASTEXITCODE -ne 0) { throw "Desktop build failed: $LASTEXITCODE" }
Get-FileHash -LiteralPath $output -Algorithm SHA256
