@echo off
REM Better Agent - Windows launcher (the macOS counterpart is run.sh).
REM Binds 127.0.0.1 only; serves the prebuilt frontend/dist from :8000.
REM Path-independent: derives its own location, so the repo can live
REM anywhere. Open the desktop shortcut that points here.
title Better Agent

set "ROOT=%~dp0"
cd /d "%ROOT%backend"

echo Stopping previous instance...
taskkill /F /IM uvicorn.exe >nul 2>&1
timeout /t 1 /nobreak >nul

echo Opening browser...
start "" "chrome.exe" "http://127.0.0.1:8000" 2>nul || start "" "http://127.0.0.1:8000"

echo Starting Better Agent backend on http://127.0.0.1:8000 ...
for /f "delims=" %%i in ('py dependency_plan.py activate --uv uv') do set "ACTIVE_ENV=%%i"
if not defined ACTIVE_ENV (
  echo Backend dependency activation failed.
  exit /b 1
)
"%ACTIVE_ENV%\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
pause
