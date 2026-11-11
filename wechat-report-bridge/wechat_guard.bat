@echo off
chcp 65001 >nul
cd /d "%~dp0.."
rem v2: 心跳 60s（供飞书监控判活）/ 内存探测 300s / 无害保活 15min
"%~dp0..\runtime\python313\python.exe" -X utf8 "%~dp0wechat_guard.py" --interval 60 --probe-every 300 --nudge-minutes 15
echo.
echo Wechat guard stopped. Press any key to close.
pause >nul
