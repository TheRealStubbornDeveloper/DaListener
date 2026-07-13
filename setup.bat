@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.12 -m venv .venv
  ) else (
    where python >nul 2>nul
    if not errorlevel 1 (
      python -m venv .venv
    ) else if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
      "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m venv .venv
    ) else (
      echo Python 3.11 or newer is required. Install it from python.org, then run setup.bat again.
      pause
      exit /b 1
    )
  )
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -e .[test]
echo.
echo DaListener is ready. Run run.bat to start it.
pause
