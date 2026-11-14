param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$work = Join-Path $PSScriptRoot '.test-data\windows-build'
New-Item -ItemType Directory -Path $work -Force | Out-Null
& $Python -m PyInstaller --noconfirm --onedir --name research-server `
    --distpath (Join-Path $PSScriptRoot 'runtime') --workpath $work --specpath $work `
    --add-data "$(Join-Path $PSScriptRoot 'static'):static" --copy-metadata pypdf --collect-all pypdfium2 `
    --collect-all pypdfium2_raw (Join-Path $PSScriptRoot 'app.py')
if ($LASTEXITCODE -ne 0) { throw 'Bundled server build failed.' }

$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319'
    $compiler = Join-Path $framework 'csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw '.NET Framework 4.x csc.exe is required to build the launcher.' }
$output = Join-Path $PSScriptRoot 'ResearchAgent.exe'
& $compiler /nologo /target:winexe /optimize+ /codepage:65001 "/out:$output" `
    "/win32icon:$(Join-Path $PSScriptRoot 'windows\ResearchAgent.ico')" `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll `
    (Join-Path $PSScriptRoot 'windows\ResearchAgent.cs')
if ($LASTEXITCODE -ne 0) { throw 'ResearchAgent.exe build failed.' }
Write-Host $output
