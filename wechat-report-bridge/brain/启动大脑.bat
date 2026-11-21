@echo off
chcp 65001 >nul
rem 拟人大脑：手动控制台启动（带窗口，方便看日志）。
rem 与 alert_monitor 同一层级，独立进程，不碰 hook 和任何服务。
"%~dp0..\..\runtime\python313\python.exe" -X utf8 "%~dp0brain.py" %*
pause
