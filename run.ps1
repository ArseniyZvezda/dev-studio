<#
.SYNOPSIS
    Start Voxarium Studio on fixed ports, restarting it if it is already running.

.DESCRIPTION
    Servers are launched detached (see AGENTS.md - uvicorn and Vite never exit,
    so they must not be run as a tracked foreground/background task), their PIDs are
    written to .scratch/run/ and their output to .scratch/run/*.log.

    "Restart" means: whatever this script started last time is killed, and so is any
    orphan still holding one of the fixed ports - but only if that orphan is a Python or
    Node process. Anything else holding the port is reported and the script stops, rather
    than killing a stranger's process.

    Default (production shape): one process, one port - the backend serves the SPA built
    into backend/voxarium_studio/static.
    -Dev: backend with CORS for Vite, plus the Vite dev server with HMR on :5173.

    With no -Project the backend opens whatever project was open last, and the
    built-in demo project the first time - see voxarium_studio.core.workspace (section 5.1).

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Dev
    .\run.ps1 -Project D:\Games\MyGame -Port 8765
    .\run.ps1 -Stop
#>
[CmdletBinding()]
param(
    # Managed project to open. Defaults to $env:VOXARIUM_STUDIO_PROJECT; with neither, the
    # backend opens the last project it had open, or the built-in demo project.
    [string]$Project,
    [int]$Port = 8765,
    [int]$VitePort = 5173,
    # Run the Vite dev server too, and start the backend with --dev CORS.
    [switch]$Dev,
    # Rebuild the SPA into the Python package before starting.
    [switch]$Build,
    # Stop whatever is running and exit.
    [switch]$Stop,
    [switch]$NoOpen,
    # Electron never evicts an existing listener (dev spec §2.7).
    [switch]$Desktop
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PSModulePath = "$PSHOME\Modules;$env:PSModulePath"

$Root    = $PSScriptRoot
$RunDir  = Join-Path $Root '.scratch\run'
$Python  = Join-Path $Root 'backend\.venv\Scripts\python.exe'
if ($env:VOXARIUM_STUDIO_RUN_DIR) { $RunDir = $env:VOXARIUM_STUDIO_RUN_DIR }
$BackendDir = Join-Path $Root 'backend'
if (Test-Path (Join-Path $Root 'python\python.exe')) {
    $Python = Join-Path $Root 'python\python.exe'
    $BackendDir = $Root
    $env:PATH = "$(Join-Path $Root 'node');$(Join-Path $Root 'python\Scripts');$env:PATH"
}

function Write-Step($message)  { Write-Host "==> $message" -ForegroundColor Cyan }
function Write-Note($message)  { Write-Host "    $message" -ForegroundColor DarkGray }

# --- process handling -------------------------------------------------------------

# npm.cmd spawns node as a child, so killing the recorded PID alone leaves the server
# running. Walk the tree children-first.
function Stop-Tree([int]$ProcessId) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) { Stop-Tree ([int]$child.ProcessId) }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Stop-Recorded([string]$Name) {
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (-not (Test-Path $pidFile)) { return $false }

    $stopped = $false
    $recorded = (Get-Content $pidFile -Raw).Trim()
    if ($recorded -match '^\d+$') {
        $process = Get-Process -Id ([int]$recorded) -ErrorAction SilentlyContinue
        if ($process) {
            Write-Note "stopping previous $Name (PID $recorded)"
            Stop-Tree ([int]$recorded)
            $stopped = $true
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    return $stopped
}

# Anything still listening after the recorded PIDs are gone is an orphan from an earlier
# session (or a foreign process - which we refuse to touch).
function Stop-PortListener([int]$TargetPort, [string]$Label) {
    $owners = @()
    try {
        $owners = @(Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction Stop |
                    Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        # Get-NetTCPConnection throws when nothing is listening.
        return $false
    }

    $stopped = $false
    foreach ($owner in $owners) {
        $process = Get-Process -Id $owner -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        if ($process.ProcessName -notin @('python', 'pythonw', 'node', 'npm', 'cmd', 'uvicorn')) {
            Write-Host "Port $TargetPort ($Label) is held by $($process.ProcessName) (PID $($process.Id)), which this script did not start." -ForegroundColor Red
            Write-Host "Stop it yourself, or pick another port." -ForegroundColor Red
            exit 1
        }
        Write-Note "stopping orphan on :$TargetPort ($($process.ProcessName), PID $($process.Id))"
        Stop-Tree ([int]$process.Id)
        $stopped = $true
    }
    return $stopped
}

function Test-PortListening([int]$TargetPort) {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $probe.ConnectAsync('127.0.0.1', $TargetPort)
        return ($connect.Wait(150) -and $probe.Connected)
    } catch { return $false }
    finally { $probe.Dispose() }
}

function Stop-Everything {
    $any = $false
    if (Stop-Recorded 'vite')    { $any = $true }
    if (Stop-Recorded 'backend') { $any = $true }
    # Give the sockets a moment to leave LISTEN before we check for orphans.
    if ($any) { Start-Sleep -Milliseconds 400 }
    if (Stop-PortListener $Port     'backend') { $any = $true }
    if (Stop-PortListener $VitePort 'vite')    { $any = $true }
    if ($any) { Start-Sleep -Milliseconds 400 }
    return $any
}

function Show-Log([string]$Name) {
    foreach ($stream in @('err', 'out')) {
        $log = Join-Path $RunDir "$Name.$stream.log"
        if ((Test-Path $log) -and (Get-Item $log).Length -gt 0) {
            Write-Host "--- $Name.$stream.log (tail) ---" -ForegroundColor DarkYellow
            Get-Content $log -Tail 20 | ForEach-Object { Write-Host "    $_" }
        }
    }
}

# --- stop-only mode ---------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

if ($Stop) {
    Write-Step 'Stopping Voxarium Studio'
    if (Stop-Everything) { Write-Host 'Stopped.' -ForegroundColor Green }
    else                 { Write-Host 'Nothing was running.' -ForegroundColor Green }
    exit 0
}

# --- resolve the managed project --------------------------------------------------

# Nothing named here is not an error: the path is simply left off the command line and
# `voxarium_studio run` opens the project it had open last, or the built-in demo project. Passing
# .scratch\demo-project, which this used to default to, is now how you ask for it by name.
if (-not $Project -and $env:VOXARIUM_STUDIO_PROJECT) { $Project = $env:VOXARIUM_STUDIO_PROJECT }

if ($Project) {
    $requested = $Project
    $Project = (Resolve-Path $requested -ErrorAction SilentlyContinue).Path
    if (-not $Project) {
        Write-Host "No such directory: $requested" -ForegroundColor Red
        exit 2
    }
}

if (-not (Test-Path $Python)) {
    Write-Host "Backend virtualenv missing: $Python" -ForegroundColor Red
    Write-Host 'Create it first:  cd backend; uv pip install -e ".[teams,dev]"' -ForegroundColor Red
    exit 2
}

# --- optional rebuild of the SPA ---------------------------------------------------

if ($Build) {
    Write-Step 'Building the SPA into backend/voxarium_studio/static'
    Push-Location (Join-Path $Root 'frontend')
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { Write-Host 'npm run build failed.' -ForegroundColor Red; exit 1 }
    } finally { Pop-Location }
}

if (-not $Dev -and -not $Desktop -and -not (Test-Path (Join-Path $Root 'backend\voxarium_studio\static\index.html'))) {
    Write-Host 'The SPA is not built, so the backend will serve 503 at /.' -ForegroundColor Yellow
    Write-Host 'Run this script with -Build, or with -Dev for the Vite dev server.' -ForegroundColor Yellow
}

# --- restart ------------------------------------------------------------------------

Write-Step 'Clearing previous instances'
if ($Desktop) {
    if (Test-PortListening $Port) {
        Write-Host "Port $Port is already occupied."
        exit 2
    }
} elseif (-not (Stop-Everything)) { Write-Note 'nothing was running' }

# --- backend -------------------------------------------------------------------------

Write-Step "Starting the backend on :$Port"
if ($Project) { Write-Note "project $Project" }
else          { Write-Note 'project the last one opened, or the built-in demo project' }

$backendArgs = @('-m', 'voxarium_studio.cli', 'run')
if ($Project) { $backendArgs += ('"' + $Project + '"') }
$backendArgs += @('--host', '127.0.0.1', '--port', "$Port", '--no-open')
if ($Dev) { $backendArgs += '--dev' }

$backend = Start-Process -FilePath $Python -ArgumentList $backendArgs `
    -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $RunDir 'backend.out.log') `
    -RedirectStandardError  (Join-Path $RunDir 'backend.err.log')

Set-Content -Path (Join-Path $RunDir 'backend.pid') -Value $backend.Id -Encoding utf8

$healthy = $false
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
    if ($backend.HasExited) { break }
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}

if (-not $healthy) {
    Write-Host "The backend did not answer /healthz on :$Port." -ForegroundColor Red
    Show-Log 'backend'
    Stop-Recorded 'backend' | Out-Null
    exit 1
}
Write-Note "healthy - PID $($backend.Id)"

# --- vite ------------------------------------------------------------------------------

$viteId = $null
if ($Dev) {
    Write-Step "Starting the Vite dev server on :$VitePort"

    # The port is fixed in vite.config.ts (strictPort), because the backend's --dev CORS
    # allowlist names it. -VitePort only affects what this script waits for and opens.
    $vite = Start-Process -FilePath 'npm.cmd' -ArgumentList @('run', 'dev') `
        -WorkingDirectory (Join-Path $Root 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $RunDir 'vite.out.log') `
        -RedirectStandardError  (Join-Path $RunDir 'vite.err.log')

    Set-Content -Path (Join-Path $RunDir 'vite.pid') -Value $vite.Id -Encoding utf8
    $viteId = $vite.Id

    $listening = $false
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if ($vite.HasExited) { break }
        if (Test-PortListening $VitePort) { $listening = $true; break }
        Start-Sleep -Milliseconds 500
    }

    if (-not $listening) {
        Write-Host "Vite did not start listening on :$VitePort." -ForegroundColor Red
        Show-Log 'vite'
        Stop-Recorded 'vite' | Out-Null
        Stop-Recorded 'backend' | Out-Null
        exit 1
    }
    Write-Note "listening - PID $viteId"
}

# --- done --------------------------------------------------------------------------------

if ($Dev) { $url = "http://localhost:$VitePort" } else { $url = "http://127.0.0.1:$Port" }

Write-Host ''
Write-Host "Voxarium Studio is up:  $url" -ForegroundColor Green
if ($Project) { Write-Host "  project   $Project" }
else           { Write-Host '  project   the last one opened, or the built-in demo project' }
Write-Host "  backend   PID $($backend.Id) on :$Port"
if ($viteId) { Write-Host "  vite      PID $viteId on :$VitePort" }
Write-Host "  logs      $RunDir"
Write-Host "  stop      .\run.ps1 -Stop"
Write-Host ''
Write-Host 'These processes are detached and outlive this window.' -ForegroundColor DarkGray

if (-not $NoOpen) { Start-Process $url | Out-Null }
