@echo off
setlocal
cd /d "%~dp0"

rem Development launcher. Keep this file ASCII-only:
rem cmd.exe reads .bat files using the local ANSI code page.

set "PYTHON_EXE="
set "PYTHON_ARGS="
set "BUNDLED=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

if defined REIMBURSE_PYTHON set "PYTHON_EXE=%REIMBURSE_PYTHON%"
if not defined PYTHON_EXE if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%BUNDLED%" set "PYTHON_EXE=%BUNDLED%"

if not defined PYTHON_EXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=py"
        set "PYTHON_ARGS=-3"
    )
)

if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
    echo.
    echo Python not found.
    echo Install Python 3.12, or edit PYTHON_EXE in this file.
    echo.
    pause
    exit /b 1
)

echo Starting Reimbursement Ledger (development)...
echo Python: "%PYTHON_EXE%"
echo.

"%PYTHON_EXE%" %PYTHON_ARGS% app.py
if errorlevel 1 (
    echo.
    echo The application exited with an error. See the message above.
    pause
)
endlocal
