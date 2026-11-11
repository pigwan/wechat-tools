@echo off
chcp 65001 >nul
"%~dp0..\runtime\python313\python.exe" "%~dp0launcher.py" both
echo.
echo Bridge stopped. Press any key to close.
pause >nul
