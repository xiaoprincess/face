@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" goto install_deps
python -m venv "%~dp0.venv"
:install_deps
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt"
pause
