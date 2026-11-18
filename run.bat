@echo off
rem wot-ban launcher: locate the shared workspace runtime and start the GUI.
setlocal
set "PROJECT_DIR=%~dp0"
set "RUNTIME_PY=%PROJECT_DIR%..\runtime\python.exe"
set "RUNTIME_PYW=%PROJECT_DIR%..\runtime\pythonw.exe"
cd /d "%PROJECT_DIR%"

if not exist "%RUNTIME_PY%" (
    echo [ERROR] Shared Python runtime not found:
    echo         %RUNTIME_PY%
    echo         This project expects the workspace runtime at ..\runtime\python.exe
    pause
    exit /b 1
)

"%RUNTIME_PY%" -c "import PySide6" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PySide6 is not installed in the shared runtime.
    echo         Install it with:
    echo           "%RUNTIME_PY%" -m pip install PySide6 -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
)

if exist "%RUNTIME_PYW%" (
    start "" "%RUNTIME_PYW%" "%PROJECT_DIR%main.py" %*
    exit /b 0
)

"%RUNTIME_PY%" "%PROJECT_DIR%main.py" %*
set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
