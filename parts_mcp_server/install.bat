@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto :failed
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :failed
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :failed
echo.
echo 安装完成。请确认 .env 中已配置小智 MCP 地址，再运行 start_mcp.bat。
pause
exit /b 0
:failed
echo 安装失败，请检查 Python 3 是否已安装且网络可用。
pause
exit /b 1
