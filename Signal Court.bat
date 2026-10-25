@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The local Python environment is missing. Run uv sync once during setup.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\console.ps1"
if errorlevel 1 pause
