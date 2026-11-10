@echo off
REM ============================================================
REM  Athena setup: installs the Python core deps + Electron app
REM ============================================================
title Athena setup
cd /d "%~dp0"

set PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if not exist "%PYTHON%" set PYTHON=python

echo [1/4] Python: %PYTHON%
"%PYTHON%" --version || (
  echo [ERROR] Python 3.11 not found. Install it or edit PYTHON in this file.
  pause & exit /b 1
)

echo.
echo [2/4] Installing Athena core packages...
"%PYTHON%" -m pip install -r core\requirements.txt || (
  echo [ERROR] pip install failed.
  pause & exit /b 1
)

echo.
echo [3/4] Installing the Electron app...
where npm >nul 2>nul || (
  echo [ERROR] Node.js is not installed. Get the LTS from https://nodejs.org then run setup.bat again.
  pause & exit /b 1
)
pushd app
call npm install || (
  popd
  echo [ERROR] npm install failed.
  pause & exit /b 1
)

echo.
echo [4/4] Downloading the Electron runtime...
REM Newer npm versions skip package install scripts, so run Electron's download step ourselves.
if not exist "node_modules\electron\dist\electron.exe" (
  node node_modules\electron\install.js || (
    popd
    echo [ERROR] Could not download Electron. Check your internet connection and run setup.bat again.
    pause & exit /b 1
  )
)
if not exist "node_modules\electron\dist\electron.exe" (
  popd
  echo [ERROR] Electron download did not produce electron.exe. Delete app\node_modules and run setup.bat again.
  pause & exit /b 1
)
popd

echo.
echo ============================================================
echo  Done. Start Athena with start_athena.bat
echo  (The voice server deps are installed separately by
echo   voice-server\start_server.bat the first time.)
echo ============================================================
pause
