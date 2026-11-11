@echo off
chcp 65001 >nul
"%~dp0..\runtime\python313\python.exe" "%~dp0admin\app.py" --open
echo.
echo Admin server stopped. Press any key to close.
pause >nul
