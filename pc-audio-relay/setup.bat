@echo off
rem One-time setup: creates a private Python environment and installs dependencies.
cd /d "%~dp0"
py -3.12 --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 was not found.
  echo Install it with:   winget install Python.Python.3.12
  echo Then close this window, open a new one, and run setup.bat again.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Install failed. Check your internet connection and try again.
  pause
  exit /b 1
)
echo.
echo Setup complete. Start the app with start.bat
pause
