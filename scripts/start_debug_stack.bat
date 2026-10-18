@echo off
setlocal
rem Binocle safety-log Agent launcher
rem Double-click to start; arguments: start, stop, restart, or status

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

set "PS1=%~dp0debug_stack.ps1"
if not exist "%PS1%" (
  echo [fail] Missing launcher: %PS1%
  pause
  exit /b 1
)

echo [info] Binocle safety-log Agent: %ACTION%
set "PS_EXE=powershell.exe"
echo [info] powershell = %PS_EXE%
%PS_EXE% -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Action "%ACTION%" %2 %3 %4
set "CODE=%ERRORLEVEL%"

if "%CODE%"=="0" (
  echo [ok] Done
) else (
  echo [fail] Failed with exit code %CODE%. Check results\debug_stack\ logs.
)
pause
exit /b %CODE%
