@echo off
chcp 65001 >nul
set PYTHONDONTWRITEBYTECODE=1
"%~dp0..\..\.venv\Scripts\python.exe" -X utf8 -B "%~dp0cli.py"
if errorlevel 1 pause
