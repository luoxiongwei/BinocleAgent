@echo off
setlocal
rem First-time setup for Binocle Agent.
rem Run this once on a new Windows machine before the launcher.

set "ROOT=%~dp0.."
pushd "%ROOT%"

where python >nul 2>nul
if errorlevel 1 (
  echo [fail] Python was not found. Install Python 3.12 and retry.
  popd
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [step] Creating local Python environment...
  python -m venv .venv
  if errorlevel 1 goto :failed
)

echo [step] Installing Python dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :failed

where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo [fail] npm was not found. Install Node.js 20 or newer and retry.
  goto :failed
)

echo [step] Installing frontend dependencies...
pushd frontend
npm.cmd ci
if errorlevel 1 (
  popd
  goto :failed
)
popd

if not exist config.json (
  copy /y config.example.json config.json >nul
  echo [info] Created config.json from config.example.json.
  echo [info] Fill in model/API settings before running VLM or LLM steps.
)

echo [ok] Environment prepared. Run ..\启动Binocle安全日志Agent.bat
popd
pause
exit /b 0

:failed
echo [fail] Setup failed. Check the message above and retry.
popd
pause
exit /b 1
