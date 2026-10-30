@echo off
rem Starts the work-history app in the background (pythonw: no console window to close by mistake).
rem Stop it with the Quit button in the app (Privacy tab). Errors appear in a message box.
setlocal
cd /d "%~dp0"
rem Lets the app point autostart / shortcuts at this launcher.
set "WORKHISTORY_LAUNCHER=%~f0"

rem Prefer the project venv (it has the optional AI SDK), else the system Python.
set "PY="
if exist ".venv\Scripts\pythonw.exe" set "PY=.venv\Scripts\pythonw.exe"
if not defined PY (
  where pythonw >nul 2>nul
  if errorlevel 1 (set "PY=pyw -3") else (set "PY=pythonw")
)
start "" %PY% -m workhistory %*
