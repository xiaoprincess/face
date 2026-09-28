@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" goto start_app
echo Please run install.bat first.
pause
exit /b 1
:start_app
"%~dp0.venv\Scripts\python.exe" "%~dp0app.py"
pause
