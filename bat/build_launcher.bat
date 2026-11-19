@echo off
rem wot-ban thin launcher builder: original tank icon + C# WinExe shell.
rem Steps: draw multi-size ICO -> generate C# source -> compile with csc.exe
rem        -> deploy the Chinese-named launcher exe into the project root.
rem
rem This bat lives in <project>\bat\; the project root is its parent folder.
rem
rem Usage:
rem   bat\build_launcher.bat              build icon + exe
rem   bat\build_launcher.bat --check      check build environment only (no files written)
rem   bat\build_launcher.bat --skip-verify
rem
rem Requires any Python 3.9+ (workspace runtime ..\..\runtime\python.exe is used first
rem when present; otherwise Python launcher / python on PATH).
setlocal
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

set "PYTHON_CMD="

rem 1) workspace shared runtime (developer machine)
if exist "%~dp0..\..\runtime\python.exe" set "PYTHON_CMD="%~dp0..\..\runtime\python.exe""

rem 2) Windows Python launcher
if not defined PYTHON_CMD (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

rem 3) Python on PATH
if not defined PYTHON_CMD (
    python -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo [ERROR] Python 3.9+ was not found.
    echo         Install Python 3.12 from https://www.python.org/downloads/
    echo         or run this script from the workspace where ..\..\runtime\python.exe exists.
    pause
    exit /b 1
)

echo [info] Using Python: %PYTHON_CMD%
%PYTHON_CMD% "%PROJECT_DIR%\scripts\build_launcher.py" %*
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
