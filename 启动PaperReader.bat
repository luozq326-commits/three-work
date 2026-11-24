@echo off
setlocal
cd /d "%~dp0"
title PaperReader

set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%"

echo ==================================================
echo   PaperReader 一键启动器
echo   工作目录: %CD%
echo ==================================================

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python 命令，请确认已安装 Python 3.12 并加入 PATH。
    pause
    exit /b 1
)

python -c "import socket,sys; s=socket.socket(); r=s.connect_ex(('127.0.0.1', %PORT%)); s.close(); sys.exit(0 if r==0 else 1)"
if %errorlevel%==0 (
    echo [提示] 检测到 %URL% 已在运行，直接为您打开浏览器...
    start "" "%URL%"
    timeout /t 2 /nobreak >nul
    exit /b 0
)

echo [启动] 正在启动本地服务 %URL% ...
echo [提示] 关闭本窗口或按 Ctrl+C 即可停止服务。
echo [提示] 演示前使用本机 Ollama 时，可先运行一次 python warmup.py 省掉约 4 秒模型加载。
echo.

start "" /b python -c "import time,webbrowser; time.sleep(2); webbrowser.open('%URL%')"

python web_server.py --port %PORT%
echo.
echo [已退出] 服务已停止。
pause
