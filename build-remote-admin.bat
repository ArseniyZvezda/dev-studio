@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Build the standalone console administrator, not the Remote server.
rem Requires Python 3.12+ on PATH (or the Windows py launcher) and internet for pip.
rem Pass --no-pause for terminal/automated use.

pushd "%~dp0"
if errorlevel 1 exit /b 1
set "build_exit=1"
set "ROOT=%CD%"
if not defined VOXARIUM_REMOTE_ADMIN_BUILD_DIR set "VOXARIUM_REMOTE_ADMIN_BUILD_DIR=%ROOT%\.scratch\remote-admin-build"
for %%I in ("%VOXARIUM_REMOTE_ADMIN_BUILD_DIR%") do set "WORK=%%~fI"
if not exist "%WORK%\tmp" mkdir "%WORK%\tmp"
if errorlevel 1 goto failed
set "TEMP=%WORK%\tmp"
set "TMP=%TEMP%"
set "PIP_CACHE_DIR=%WORK%\pip-cache"
set "PYINSTALLER_CONFIG_DIR=%WORK%\pyinstaller-cache"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONPATH="
set "PYTHONHOME="

if exist "%WORK%\venv\Scripts\python.exe" goto check_venv
echo Creating an isolated build environment...
py -3 -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if errorlevel 1 goto use_python
py -3 -m venv "%WORK%\venv"
if errorlevel 1 goto failed
goto check_venv

:use_python
python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Install Python 3.12 or newer with pip and venv, then run this file again.
    goto done
)
python -m venv "%WORK%\venv"
if errorlevel 1 goto failed

:check_venv
set "PYTHON=%WORK%\venv\Scripts\python.exe"
"%PYTHON%" -c "import sys; sys.exit(sys.version_info < (3, 12))"
if errorlevel 1 (
    echo ERROR: Build environment is unusable. Set VOXARIUM_REMOTE_ADMIN_BUILD_DIR to a new directory.
    goto done
)
echo Installing build dependencies...
rem Only the CLI dependencies: no FastAPI, server runtime or Studio backend.
"%PYTHON%" -m pip install --disable-pip-version-check "typer>=0.15,<1" "prompt-toolkit>=3.0.43,<4" "pyinstaller>=6,<7"
if errorlevel 1 goto failed

rem An absolute package import is required; freezing admin.py directly breaks relative imports.
>"%WORK%\admin_entry.py" echo from voxarium_studio_remote.admin import main
if errorlevel 1 goto failed
>>"%WORK%\admin_entry.py" echo main()
if errorlevel 1 goto failed

set "ASSETS=%WORK%\assets-%RANDOM%-%RANDOM%"
echo Staging Remote source and Windows OpenSSH...
"%PYTHON%" "%ROOT%\remote\build_admin_bundle.py" --root "%ROOT%" --output "%ASSETS%"
if errorlevel 1 goto failed

echo Building voxarium-remote-admin.exe...
"%PYTHON%" -m PyInstaller --noconfirm --clean --onefile --console --name voxarium-remote-admin --paths "%ROOT%\remote" --distpath "%WORK%\dist" --workpath "%WORK%\build" --specpath "%WORK%" --add-data "%ASSETS%;admin_assets" --add-binary "%ASSETS%\openssh\*.exe;admin_assets/openssh" "%WORK%\admin_entry.py"
if errorlevel 1 goto failed
echo Checking executable startup...
"%WORK%\dist\voxarium-remote-admin.exe" --help >nul
if errorlevel 1 goto failed
"%WORK%\dist\voxarium-remote-admin.exe" --check-bundle
if errorlevel 1 goto failed
copy /y "%WORK%\dist\voxarium-remote-admin.exe" "%ROOT%\voxarium-remote-admin.exe" >nul
if errorlevel 1 goto publish_updated
echo.
echo Ready: "%ROOT%\voxarium-remote-admin.exe"
goto published

:publish_updated
copy /y "%WORK%\dist\voxarium-remote-admin.exe" "%ROOT%\voxarium-remote-admin-updated.exe" >nul
if errorlevel 1 goto failed
echo Ready: "%ROOT%\voxarium-remote-admin-updated.exe" - main EXE is in use.

:published
echo Remote source and OpenSSH are embedded; no local Python or OpenSSH is needed.
set "build_exit=0"
goto done

:failed
set "build_exit=%errorlevel%"
if "%build_exit%"=="0" set "build_exit=1"
echo.
echo ERROR: Remote Admin build failed with exit code %build_exit%. See the output above.

:done
popd
if /i not "%~1"=="--no-pause" pause
exit /b %build_exit%

