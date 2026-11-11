@echo off
chcp 65001 >nul
title WxBridge - Restore Feishu Alert
cd /d "%~dp0.."
"%~dp0..\runtime\python313\python.exe" -X utf8 "%~dp0fix_alert_stack.py"
echo.
pause
