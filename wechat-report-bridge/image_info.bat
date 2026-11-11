@echo off
chcp 65001 >nul
"%~dp0..\runtime\python313\python.exe" "%~dp0image_info.py" %*
echo.
pause >nul
