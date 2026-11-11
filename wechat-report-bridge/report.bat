@echo off
chcp 65001 >nul
"%~dp0..\runtime\python313\python.exe" "%~dp0daily_report.py" %*
echo.
pause >nul
