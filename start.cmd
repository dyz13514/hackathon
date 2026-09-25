@echo off
rem Double-click this file to start the project on Windows.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
  echo.
  echo Startup failed. Check the error above and the server windows.
  pause
)
