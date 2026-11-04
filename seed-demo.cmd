@echo off
rem Presentation: puts the fictional demo scenario into the app's own database (the old one goes to
rem %LOCALAPPDATA%\WorkHistory\backup\). See tools\seed_demo.py.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 작업 기록 앱의 기록을 시연용 데모 데이터로 바꿉니다 (지난 2주 + 오늘 07:00~09:00).
echo 지금 기록은 지우지 않고 %%LOCALAPPDATA%%\WorkHistory\backup 폴더로 옮깁니다.
echo.
choice /c YN /m "계속할까요"
if errorlevel 2 exit /b 0
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "runtime\python.exe" set "PY=runtime\python.exe"
if not defined PY set "PY=python"
%PY% "tools\seed_demo.py"
if errorlevel 1 echo 실패했습니다. 위의 메시지를 확인하세요.
pause
