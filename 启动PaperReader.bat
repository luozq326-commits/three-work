@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title PaperReader

set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%"

echo ==================================================
echo   PaperReader 一键启动
echo   目录: %CD%
echo ==================================================

REM 优先用装了依赖的 Python 3.12。
REM 注意：系统 PATH 里可能先命中别的 python（比如工具自带的 3.13），
REM 那个没装 pymupdf 等依赖，直接用会启动失败。
set "PY="
if exist "E:\Python312\python.exe" set "PY=E:\Python312\python.exe"
if not defined PY (
    py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [错误] 没找到可用的 Python，请先安装 Python 3.12
    pause
    exit /b 1
)

echo [信息] 使用解释器: !PY!

REM 依赖自检：缺了就明确告诉用户装什么，而不是启动到一半才报错
!PY! -c "import pymupdf, pymupdf4llm, rapidocr_onnxruntime" >nul 2>nul
if errorlevel 1 (
    echo [错误] 当前 Python 缺少依赖，请先执行：
    echo        !PY! -m pip install -r requirements.txt
    pause
    exit /b 1
)

REM 端口已被占用：说明服务已经在跑，直接开浏览器
!PY! -c "import socket,sys; s=socket.socket(); r=s.connect_ex(('127.0.0.1', %PORT%)); s.close(); sys.exit(0 if r==0 else 1)"
if %errorlevel%==0 (
    echo [提示] 检测到 %URL% 已在运行，直接打开浏览器...
    start "" "%URL%"
    timeout /t 2 /nobreak >nul
    exit /b 0
)

echo [信息] 正在启动服务，稍后自动打开 %URL%
echo [提示] 关闭本窗口或 Ctrl+C 即停止服务
echo.

REM 后台等服务起来再开浏览器，避免打开太快看到"无法访问"
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start """" ""%URL%"""

!PY! -u web_server.py --lan

echo.
echo [信息] 服务已停止
pause
