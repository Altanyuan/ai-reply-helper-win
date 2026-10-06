@echo off
chcp 65001 >nul
title WeChat AI Polish Assistant
cd /d %~dp0
echo ===================================================
echo   WeChat AI Polish Assistant
echo   Directory: %CD%
echo   Starting...
echo ===================================================
python main.py
if errorlevel 1 (
    echo.
    echo [ERROR] Startup failed. Please make sure:
    echo   - main.py exists in this directory
    echo   - Python is installed and added to PATH
    echo.
)
pause
