@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."
"%~dp0..\..\runtime\python313\python.exe" -X utf8 "%~dp0cli.py" %*
echo.
if "%~1"=="" pause >nul
