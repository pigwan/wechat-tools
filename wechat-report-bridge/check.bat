@echo off
chcp 65001 >nul
"%~dp0..\runtime\python313\python.exe" "%~dp0launcher.py" check
echo.
pause >nul
