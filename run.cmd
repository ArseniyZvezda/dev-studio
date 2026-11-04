@echo off
rem Double-clickable wrapper around run.ps1 - see that file for the options.
rem Everything after `run.cmd` is passed through: run.cmd -Dev, run.cmd -Stop, ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
