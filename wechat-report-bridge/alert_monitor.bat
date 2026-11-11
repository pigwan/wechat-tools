@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Starting Feishu alert monitor (keep this window open)...
"%~dp0..\runtime\python313\python.exe" -X utf8 "%~dp0health_monitor.py"
echo.
echo Alert monitor stopped. Press any key to close.
pause >nul
