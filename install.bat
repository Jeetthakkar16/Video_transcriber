@echo off
setlocal
cd /d "%~dp0"
where ffmpeg >nul 2>nul
if errorlevel 1 echo [WARNING] FFmpeg is missing from PATH. Install FFmpeg and reopen your terminal.
py -3.11 --version >nul 2>nul
if errorlevel 1 (
 echo Python 3.11 was not found. Install Python 3.11 and enable Add Python to PATH.
 pause
 exit /b 1
)
py -3.11 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Installation completed. Start the app with run.bat.
pause
exit /b 0
:fail
echo Setup failed. Review the errors above.
pause
exit /b 1
