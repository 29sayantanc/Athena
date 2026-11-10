@echo off
REM ============================================================
REM  Voice Server Launcher (TUI)
REM  Parakeet STT + Pocket TTS + Silero VAD
REM  OpenAI Realtime API Compatible
REM ============================================================

title Voice Server - Parakeet + Pocket TTS
cd /d "%~dp0"

REM --- Python 3.11 (edit this line if yours is elsewhere) ---
set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

REM --- Check Python exists ---
"%PYTHON%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found at: %PYTHON%
    echo Please edit this .bat file and set the correct PYTHON path.
    pause
    exit /b 1
)

REM --- Check requirements installed ---
echo Checking dependencies...
"%PYTHON%" -c "import textual, fastapi, torch, transformers, silero_vad, pocket_tts, multipart, numpy, soundfile" 2>nul
if errorlevel 1 (
    echo [WARN] Some dependencies missing. Installing from requirements.txt...
    "%PYTHON%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

REM --- Launch TUI ---
echo.
echo ========================================
echo   Voice Server Configuration TUI
echo   Parakeet STT + Pocket TTS + Silero VAD
echo ========================================
echo.
"%PYTHON%" -u tui.py

REM --- Keep terminal open after exit ---
echo.
echo ========================================
echo   Server TUI exited.
echo ========================================
echo.
pause
