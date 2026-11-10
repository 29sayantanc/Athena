@echo off
REM Starts Athena with a console so you can see the core's logs live.
title Athena (debug)
cd /d "%~dp0app"
"%~dp0app\node_modules\electron\dist\electron.exe" .
pause
