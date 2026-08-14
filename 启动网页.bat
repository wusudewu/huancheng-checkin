@echo off
chcp 65001 >nul
title 幻城签到 · Web 控制台
cd /d "%~dp0"

echo ============================================
echo   幻城网安签到 · Web 控制台
echo ============================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8 以上版本。
    pause
    exit /b 1
)

REM 检查依赖，缺失则自动安装
python -c "import flask, requests" >nul 2>&1
if errorlevel 1 (
    echo [提示] 正在安装依赖...
    pip install -r requirements.txt
)

echo.
echo 正在启动 Web 服务...
echo 启动后请在浏览器访问： http://127.0.0.1:5000
echo 按 Ctrl+C 可停止服务
echo.

start "" http://127.0.0.1:5000
python web_app.py

pause
