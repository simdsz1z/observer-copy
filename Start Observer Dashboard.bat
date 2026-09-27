@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 dashboard.py
) else (
  python dashboard.py
)
if errorlevel 1 (
  echo.
  echo Could not start Observer Dashboard. Make sure Python 3 is installed.
  pause
)
