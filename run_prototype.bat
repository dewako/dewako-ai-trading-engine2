@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "DEWAKO_PYTHON=%CD%\.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo ERROR: Python was not found. Install Python 3.11 or newer and try again.
    pause
    exit /b 1
  )
  set "DEWAKO_PYTHON=python"
)

"%DEWAKO_PYTHON%" scripts\run_prototype.py
if errorlevel 1 (
  echo.
  echo Dewako prototype did not start. Review the error above and the setup guide.
  pause
)
