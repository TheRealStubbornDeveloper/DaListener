@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo DaListener is not installed. Run setup.bat first.
  pause
  exit /b 1
)
start "DaListener" ".venv\Scripts\pythonw.exe" -m dalistener.dashboard.server
