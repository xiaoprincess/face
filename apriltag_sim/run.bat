@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ was not found in PATH.
  echo Install Python and enable "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)
python main.py %*
if errorlevel 1 (
  echo.
  echo The program exited with an error. Copy the message above when asking for help.
  pause
)
endlocal
