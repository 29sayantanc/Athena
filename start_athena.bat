@echo off
REM Starts Athena in the system tray (no console window stays open).
cd /d "%~dp0"
set ELECTRON=%~dp0app\node_modules\electron\dist\electron.exe
if not exist "%ELECTRON%" (
  echo Athena is not set up yet. Running setup.bat first...
  call "%~dp0setup.bat"
)
if not exist "%ELECTRON%" (
  echo [ERROR] Electron is still missing. Run setup.bat and check the messages.
  pause
  exit /b 1
)
start "" "%ELECTRON%" "%~dp0app"
