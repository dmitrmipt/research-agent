param([int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# Prefer a normal Python installation. The bundled runtime is a local fallback,
# not a dependency on a running Codex application or on its tools.
$pythonPath = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\WindowsApps\*') {
        & $command.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonPath = $command.Source; break }
    }
}
if (-not $pythonPath) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) { $pythonPath = $bundledPython }
}
if (-not $pythonPath) {
    Write-Host 'Python 3.10+ is required. Install Python from python.org and run this file again.'
    Read-Host 'Press Enter to exit'
    exit 1
}

$releaseVersion = & $pythonPath -B -c 'from version import VERSION; print(VERSION)'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$address = "http://127.0.0.1:$Port"
$health = $null
try {
    $health = Invoke-RestMethod -Uri "$address/api/health" -TimeoutSec 2
} catch { }
if ($health.app -eq 'research-agent' -and $health.worker_alive) {
    Write-Host "Research Agent $($health.version) is already running: $address"
    if ($health.version -ne $releaseVersion) {
        Write-Host "Project version: $releaseVersion. Stop the running server with Ctrl+C and run start.cmd again."
    }
    if (-not $NoBrowser) { Start-Process $address }
    exit 0
}
if ($health.mode -eq 'demo') {
    Write-Host 'An older Research Agent server is running. Stop it with Ctrl+C and run start.cmd again.'
    exit 1
}

Write-Host "Starting Research Agent $releaseVersion. Keep this window open. Ctrl+C stops the server."
& $pythonPath -c 'import pypdf, pypdfium2, markdown_it, PIL' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'PDF processing and formatted Markdown preview use packages from requirements.txt. Install them with:'
    Write-Host "& '$pythonPath' -m pip install -r requirements.txt"
}
if (-not $NoBrowser) {
    # Open only after the server is ready. This helper exits within ten seconds.
    $browserJob = Start-Job -ArgumentList $address -ScriptBlock {
        param($localAddress)
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri "$localAddress/api/health" -TimeoutSec 1
                if ($health.app -eq 'research-agent' -and $health.worker_alive) { Start-Process $localAddress; break }
            } catch { }
            Start-Sleep -Milliseconds 500
        }
    }
}
try { & $pythonPath (Join-Path $PSScriptRoot 'app.py') --port $Port }
finally {
    if ($browserJob) { Stop-Job -Job $browserJob -ErrorAction SilentlyContinue; Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue }
}
exit $LASTEXITCODE
