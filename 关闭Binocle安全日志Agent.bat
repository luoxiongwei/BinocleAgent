@echo off
rem Root double-click entry to STOP the three Binocle services.
rem Closes: agent :8000, console :8010, frontend :5173 (PID first, port fallback).
call "%~dp0scripts\start_debug_stack.bat" stop
