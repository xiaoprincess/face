@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
if errorlevel 1 (
  echo.
  echo Startup failed. See the message above.
  pause
  exit /b 1
)
echo.
echo Website and MCP are running. Starting the MCP monitor...
echo.
"%~dp0parts_mcp_server\.venv\Scripts\python.exe" -X utf8 "%~dp0parts_mcp_server\mcp_monitor.py"
if errorlevel 1 (
  echo.
  echo MCP monitor exited unexpectedly. Website and MCP remain running.
  pause
  exit /b 1
)
exit /b 0
