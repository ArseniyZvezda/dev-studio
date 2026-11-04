@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Build the Windows x64 installer using the desktop pipeline (dev spec §2.7).
rem Pass --no-pause when running from a terminal or automation.

pushd "%~dp0desktop"
if errorlevel 1 exit /b 1

for %%T in (node.exe npm.cmd uv.exe) do (
    where %%T >nul 2>&1
    if errorlevel 1 (
        echo ERROR: %%T is missing from PATH. Install Node.js 22.12+ x64 with npm and uv.
        set "build_exit=1"
        goto done
    )
)

echo Installing desktop dependencies...
call npm.cmd ci --prefer-offline --no-audit --no-fund
if errorlevel 1 goto failed

echo Running desktop tests...
call npm.cmd test
if errorlevel 1 goto failed

echo Building the Windows x64 installer...
call npm.cmd run dist -- --publish never
if errorlevel 1 goto failed

echo.
echo Installer ready in "%~dp0desktop\dist".
set "build_exit=0"
goto done

:failed
set "build_exit=%errorlevel%"
echo.
echo ERROR: Installer build failed with exit code %build_exit%. See the output above.

:done
popd
if /i not "%~1"=="--no-pause" pause
exit /b %build_exit%
