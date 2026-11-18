@echo off
rem Starts PC Audio Relay. Extra arguments are passed through, e.g.  start.bat --console
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pc_audio_relay %*
if errorlevel 1 pause
