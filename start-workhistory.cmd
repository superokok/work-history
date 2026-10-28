@echo off
rem Starts the work-history app (on Windows it records window activity itself).
rem Close this window to stop the app.
setlocal
title WorkHistory
cd /d "%~dp0"
rem Lets the app point autostart / shortcuts at this launcher.
set "WORKHISTORY_LAUNCHER=%~f0"

rem Prefer the project venv (it has the optional AI SDK), else the system Python.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 (set "PY=py -3") else (set "PY=python")
)
%PY% -m workhistory %*
if errorlevel 1 pause
