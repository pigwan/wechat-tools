@echo off
chcp 65001 >nul
title WeChat Tool - Unified Console
cd /d "%~dp0.."
"%~dp0..\runtime\python313\python.exe" -X utf8 -u "%~dp0run_all.py" %*
echo.
echo Unified console stopped. Press any key to close this window.
pause >nul
