@echo off
rem Starts ActivityWatch (if it is not running) and the work-history app.
rem Close this window to stop the app; ActivityWatch keeps running in the tray.
setlocal
title WorkHistory
cd /d "%~dp0"
rem Lets the app point autostart / shortcuts at this launcher.
set "WORKHISTORY_LAUNCHER=%~f0"

set "AW=%LOCALAPPDATA%\ActivityWatch\activitywatch\aw-qt.exe"
tasklist /FI "IMAGENAME eq aw-qt.exe" 2>nul | find /I "aw-qt.exe" >nul
if errorlevel 1 (
  if exist "%AW%" (
    start "" "%AW%"
  ) else (
    echo ActivityWatch not found at %AW%
  )
)

rem Prefer the project venv (it has the optional AI SDK), else the system Python.
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 (set "PY=py -3") else (set "PY=python")
)
%PY% -m workhistory %*
if errorlevel 1 pause
