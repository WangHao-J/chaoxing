@echo off
setlocal
set "PYTHONUTF8=1"
set "TEMP=%~dp0temp"
set "TMP=%~dp0temp"
if not exist "%TEMP%" mkdir "%TEMP%"
cd /d "%~dp0"
if /i "%~1"=="--check" goto check
start "" "%~dp0python\pythonw.exe" "%~dp0account_manager.py"
exit /b 0
:check
"%~dp0python\python.exe" "%~dp0account_manager.py" --check
exit /b %ERRORLEVEL%
