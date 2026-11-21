@echo off
rem 拟人大脑：静默无窗口常驻（与 alert_monitor_silent.bat 对齐）。
rem 配合计划任务实现开机自启；互斥体保证只有一个实例。
start "" "%~dp0..\..\runtime\python313\pythonw.exe" -X utf8 "%~dp0brain.py"
