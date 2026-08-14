@echo off
chcp 65001 >nul
title 幻城签到 - 批量签到（无暂停）
cd /d "%~dp0"

python hcnsec_auto_checkin.py --run
