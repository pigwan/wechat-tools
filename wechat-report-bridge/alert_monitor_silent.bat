@echo off
rem Feishu alert monitor (silent, no console window).
rem Delegates to launcher.py alert so startup logic lives in exactly one place.
"%~dp0..\runtime\python313\pythonw.exe" -X utf8 "%~dp0launcher.py" alert
