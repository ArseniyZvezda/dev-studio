@echo off
rem Two people on one machine, for trying online sync (dev spec 16): starts a local
rem Voxarium Studio Remote and two Studios - alice and bob - and opens a window on each.
rem Double-click it, or:  remote_test.bat --detach | stop | --fresh | --no-browser
rem See remote_test.py for what it starts, where it keeps its files, and why it never
rem touches the ports run.cmd owns.
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%backend\.venv\Scripts\python.exe"
if exist "%ROOT%python\python.exe" set "PY=%ROOT%python\python.exe"

if not exist "%PY%" (
    echo Backend virtualenv missing: %PY%
    echo Create it first:
    echo   cd backend
    echo   uv pip install -e ".[teams,dev]" -e ../remote
    if "%~1"=="" pause
    exit /b 1
)

"%PY%" "%ROOT%remote_test.py" %*
set "CODE=%ERRORLEVEL%"
rem Double-clicked, the window would close on the message that says what went wrong.
if not "%CODE%"=="0" if "%~1"=="" pause
exit /b %CODE%
