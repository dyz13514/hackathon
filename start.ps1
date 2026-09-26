# Windows launcher. Run start.cmd, or run .\start.ps1 in PowerShell.
# Keep this file ASCII so Windows PowerShell 5.1 reads it correctly.

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$venv = Join-Path $backend '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$envFile = Join-Path $root '.env'

# Native tools and child consoles should print UTF-8, including Python logs.
chcp.com 65001 > $null
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Run-Native([string]$program, [string[]]$arguments) {
    & $program @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$program failed (exit code $LASTEXITCODE)."
    }
}

function Wait-ForServer([string]$url, [System.Diagnostics.Process]$process, [string]$label) {
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        $process.Refresh()
        if ($process.HasExited) {
            throw "$label exited before it became ready. See its PowerShell window."
        }
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { return }
        }
        catch { }
        Start-Sleep -Seconds 1
    }
    throw "$label did not become ready at $url. See its PowerShell window."
}

function Start-Server([string]$command) {
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-EncodedCommand', $encoded
    ) -PassThru
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.11+ is required. Install Python and reopen this terminal.'
}
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) {
    throw 'Node.js 18+ is required. Install Node.js and reopen this terminal.'
}

if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envFile
    Write-Host 'Created .env. Set SESSION_SHARED_PASSWORD and SESSION_SECRET_KEY before sharing this machine.'
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host 'Creating Python environment...'
    Run-Native 'python' @('-m', 'venv', $venv)
}

& $python -c 'import alembic, fastapi, uvicorn'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing backend dependencies...'
    Run-Native $python @('-m', 'pip', 'install', '-e', "${backend}[dev]")
}

if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules'))) {
    Write-Host 'Installing frontend dependencies...'
    Push-Location $frontend
    try { Run-Native $npm @('install') } finally { Pop-Location }
}

Write-Host 'Preparing database...'
Push-Location $backend
try {
    Run-Native $python @('-m', 'alembic', 'upgrade', 'head')
}
finally { Pop-Location }

# Stop here if another process already owns either fixed development port.
foreach ($port in @(8000, 5173)) {
    $client = New-Object System.Net.Sockets.TcpClient
    $inUse = $false
    try {
        $client.Connect('127.0.0.1', $port)
        $inUse = $true
    }
    catch { }
    finally { $client.Dispose() }
    if ($inUse) {
        throw "Port $port is already in use. Close the old development server and retry."
    }
}

$utf8Setup = 'chcp.com 65001 > $null; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); $OutputEncoding = [Console]::OutputEncoding; $env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"'
$backendPath = $backend.Replace("'", "''")
$frontendPath = $frontend.Replace("'", "''")
$pythonPath = $python.Replace("'", "''")
$npmPath = $npm.Replace("'", "''")

Write-Host 'Starting backend...'
$backendProcess = Start-Server "$utf8Setup; Set-Location -LiteralPath '$backendPath'; & '$pythonPath' -m uvicorn app.main:create_app --factory --reload --workers 1 --port 8000"
Wait-ForServer 'http://127.0.0.1:8000/health' $backendProcess 'Backend'

Write-Host 'Starting frontend...'
$frontendProcess = Start-Server "$utf8Setup; Set-Location -LiteralPath '$frontendPath'; & '$npmPath' run dev -- --host 127.0.0.1 --port 5173 --strictPort"
Wait-ForServer 'http://127.0.0.1:5173/' $frontendProcess 'Frontend'

Write-Host 'Ready: http://localhost:5173'
Start-Process 'http://localhost:5173'
