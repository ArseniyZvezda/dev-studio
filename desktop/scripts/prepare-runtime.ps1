# Build the self-contained Windows distribution (dev spec §2.7).
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PSModulePath = "$PSHOME\Modules;$env:PSModulePath"
$DesktopRoot = Split-Path $PSScriptRoot -Parent
$RepoRoot = Split-Path $DesktopRoot -Parent
$Runtime = Join-Path $DesktopRoot 'build\runtime'
function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed ($LASTEXITCODE)" }
}
Push-Location $RepoRoot
try {
    $Frontend = Join-Path $RepoRoot 'frontend'
    $BuildRoot = Join-Path $DesktopRoot 'build'
    New-Item -ItemType Directory -Force $BuildRoot | Out-Null
    $ReleaseJson = & node.exe (Join-Path $PSScriptRoot 'release.cjs')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the Studio build version' }
    $Release = $ReleaseJson | ConvertFrom-Json
    $LockHash = (Get-FileHash (Join-Path $Frontend 'package-lock.json')).Hash
    $DepsStamp = Join-Path $Frontend 'node_modules/.studio-lock'
    if (-not (Test-Path $DepsStamp) -or (Get-Content $DepsStamp -Raw) -ne $LockHash) {
        Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'ci', '--prefer-offline', '--no-audit', '--no-fund')
        Set-Content -NoNewline -LiteralPath $DepsStamp -Value $LockHash
    }
    $SourceDirs = @('src', 'public' | ForEach-Object { Join-Path $Frontend $_ } | Where-Object { Test-Path -LiteralPath $_ })
    $Sources = @(Get-ChildItem -LiteralPath $SourceDirs -Recurse -File)
    $Sources += @(Get-ChildItem $Frontend -File)
    $DigestText = ($Sources | Sort-Object FullName | ForEach-Object { $_.FullName + ':' + (Get-FileHash -LiteralPath $_.FullName).Hash }) -join "`n"
    # Adding/removing a tag changes the compiled version without touching source files (§2.5).
    $DigestText += "`nStudio version: $($Release.version)"
    $SourceHash = [Convert]::ToBase64String([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($DigestText)))
    $SpaStamp = Join-Path $BuildRoot 'frontend.hash'
    if (-not (Test-Path $SpaStamp) -or (Get-Content $SpaStamp -Raw) -ne $SourceHash -or -not (Test-Path 'backend/voxarium_studio/static/index.html')) {
        Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', 'build')
        Set-Content -NoNewline -LiteralPath $SpaStamp -Value $SourceHash
    }
    New-Item -ItemType Directory -Force $Runtime | Out-Null
    $BundledPython = Join-Path $Runtime 'python\python.exe'
    if (-not (Test-Path $BundledPython)) {
        $PythonStage = Join-Path $DesktopRoot 'build\python-download'
        Invoke-Checked 'uv' @('python', 'install', '3.12', '--install-dir', $PythonStage, '--no-bin')
        $Python = Get-ChildItem $PythonStage -Filter python.exe -Recurse | Select-Object -First 1
        if (-not $Python) { throw 'Python runtime was not downloaded' }
        Copy-Item -LiteralPath $Python.Directory.FullName -Destination (Join-Path $Runtime 'python') -Recurse
    }
    # A wheel (not an editable install) includes the SPA, built-in plugins and build stamp.
    $Constraints = Join-Path $BuildRoot 'runtime-requirements.txt'
    Invoke-Checked 'uv' @('export', '--project', 'backend', '--frozen', '--extra', 'teams', '--no-dev', '--no-emit-project', '--no-hashes', '--output-file', $Constraints)
    Invoke-Checked 'uv' @('pip', 'install', '--python', $BundledPython, '--break-system-packages', '--reinstall-package', 'voxarium-studio', '--compile-bytecode', '--constraint', $Constraints, './backend[teams]')
    Copy-Item run.cmd, run.ps1 -Destination $Runtime
    $NodeDir = Join-Path $Runtime 'node'
    New-Item -ItemType Directory -Force $NodeDir | Out-Null
    $Node = (Get-Command node.exe).Source
    Invoke-Checked $Node @('-e', "if(process.arch!=='x64')process.exit(1)")
    Copy-Item -LiteralPath $Node -Destination $NodeDir
    # npm is used by plugins that provision JavaScript dependencies.
    $NodeSource = Split-Path $Node -Parent
    Copy-Item (Join-Path $NodeSource 'npm.cmd'), (Join-Path $NodeSource 'npx.cmd') -Destination $NodeDir
    New-Item -ItemType Directory -Force (Join-Path $NodeDir 'node_modules') | Out-Null
    if (-not (Test-Path (Join-Path $NodeDir 'node_modules\npm'))) {
        Copy-Item -LiteralPath (Join-Path $NodeSource 'node_modules\npm') -Destination (Join-Path $NodeDir 'node_modules\npm') -Recurse
    }
    New-Item -ItemType Directory -Force (Join-Path $DesktopRoot 'locales') | Out-Null
    Copy-Item frontend/src/locales/en.json, frontend/src/locales/ru.json -Destination (Join-Path $DesktopRoot 'locales')
    # Verify only the installed runtime, without checkout/PYTHONPATH fallbacks (§2.7).
    Invoke-Checked $BundledPython @('-I', '-c', 'import json, sys; import voxarium_studio.cli, claude_agent_sdk; from voxarium_studio.main import static_dir; from voxarium_studio.plugins.loader import builtin_plugins_dir; from voxarium_studio.build_info import build_info; assert static_dir(); assert builtin_plugins_dir(); actual = build_info().version; frontend = json.loads((static_dir() / ''studio-version.json'').read_text())[''version'']; assert actual == frontend == sys.argv[1], (actual, frontend, sys.argv[1])', $Release.version)
} finally { Pop-Location }
