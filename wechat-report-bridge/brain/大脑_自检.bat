@echo off
chcp 65001 >nul
"%~dp0..\..\runtime\python313\python.exe" -X utf8 "%~dp0brain.py" --selftest
echo.
pause
