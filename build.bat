@echo off
chcp 65001 >nul
title Build WeChat AI Polish Assistant
cd /d %~dp0
echo ===================================================
echo   Build: PyInstaller (exe) + Inno Setup (installer)
echo   Directory: %CD%
echo ===================================================
echo.
echo [1/3] Checking PyInstaller...
python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo       Installing PyInstaller...
    python -m pip install --upgrade pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        goto :fail
    )
)

echo [2/3] Building single-file exe with PyInstaller...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
python -m PyInstaller build.spec --noconfirm --clean
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    goto :fail
)
echo       OK: dist\微信AI润色助手.exe

echo [3/3] Building installer with Inno Setup...
rem ISCC is often missing from PATH (especially for per-user installs),
rem so look in PATH first, then try the common install locations below.
rem NOTE: keep this file pure ASCII - cmd.exe mangles UTF-8 comments and
rem would swallow the "rem" keyword, printing "not recognized as a command".
set "ISCC_EXE="
for /f "delims=" %%i in ('where ISCC 2^>nul') do if not defined ISCC_EXE set "ISCC_EXE=%%i"
if not defined ISCC_EXE if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC_EXE=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC_EXE (
    echo       [SKIP] ISCC not found - installer not built.
    echo       Install Inno Setup 6 from https://jrsoftware.org/isdl.php
    echo       then re-run this script.
    goto :done
)
echo       Using ISCC: %ISCC_EXE%
"%ISCC_EXE%" installer.iss
if errorlevel 1 (
    echo [ERROR] Inno Setup compile failed.
    goto :fail
)
echo       OK: dist\Setup_*.exe

:done
echo.
echo ===================================================
echo   Build finished. Artifacts are in the dist folder.
echo ===================================================
pause
exit /b 0

:fail
echo.
echo [FAILED] See messages above.
pause
exit /b 1
