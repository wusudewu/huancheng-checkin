@echo off
chcp 65001 >nul
title 幻城网安 API 自动签到工具
cd /d "%~dp0"

echo ============================================
echo    幻城网安 API 自动签到工具
echo ============================================
echo.

python hcnsec_auto_checkin.py %*

echo.
pause
