@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Restarting WeChat guard (v2: low-frequency probe + offline forensics)...
"%~dp0..\runtime\python313\python.exe" -X utf8 "%~dp0guard_switch.py"
echo.
echo Done. Close this window; the new guard runs in its own console window.
pause >nul
