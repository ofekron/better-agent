@echo off
REM Better Agent - Windows launcher (the macOS counterpart is run.sh).
REM Binds 127.0.0.1 only; serves the prebuilt frontend/dist from :8000.
REM Path-independent: derives its own location, so the repo can live
REM anywhere. Open the desktop shortcut that points here.
title Better Agent

set "SERVICE_CHILD=0"
if /I "%~1"=="--service-child" set "SERVICE_CHILD=1"

set "ROOT=%~dp0"
cd /d "%ROOT%backend"

echo Stopping previous instance...
set "PYTHONPATH=%ROOT%;%ROOT%backend;%ROOT%desktop"

if not defined BETTER_AGENT_BACKEND_PORT set "BETTER_AGENT_BACKEND_PORT=8000"
if not defined BETTER_CLAUDE_BACKEND_PORT set "BETTER_CLAUDE_BACKEND_PORT=%BETTER_AGENT_BACKEND_PORT%"

if "%SERVICE_CHILD%"=="0" (
  echo Opening browser...
  start "" "chrome.exe" "http://127.0.0.1:%BETTER_AGENT_BACKEND_PORT%" 2>nul || start "" "http://127.0.0.1:%BETTER_AGENT_BACKEND_PORT%"
)

echo Starting Better Agent backend on http://127.0.0.1:%BETTER_AGENT_BACKEND_PORT% ...
for /f "delims=" %%i in ('py dependency_plan.py activate --uv uv') do set "ACTIVE_ENV=%%i"
if not defined ACTIVE_ENV (
  echo Backend dependency activation failed.
  exit /b 1
)
"%ACTIVE_ENV%\Scripts\python.exe" -m desktop.windows_source_launcher --checkout "%ROOT%" --host 127.0.0.1 --port %BETTER_AGENT_BACKEND_PORT%
exit /b %ERRORLEVEL%
