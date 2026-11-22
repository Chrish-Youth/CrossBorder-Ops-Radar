@echo off
setlocal EnableExtensions DisableDelayedExpansion

cd /d "%~dp0"
title CrossBorder Ops Radar

echo.
echo CrossBorder Ops Radar demo launcher
echo ===================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Python virtual environment was not found.
    echo Expected: %CD%\.venv\Scripts\python.exe
    echo.
    echo Open PowerShell in this folder and install the project dependencies first.
    pause
    exit /b 1
)

echo [OK] Python virtual environment found.

if /i "%~1"=="--check" (
    echo [OK] Launcher check completed. Streamlit was not started.
    exit /b 0
)

set "STREAMLIT_BROWSER_GATHER_USAGE_STATS=false"

echo.
echo Starting Streamlit. Keep this window open during the demo.
echo To stop the app, return here and press Ctrl+C.
echo.

".venv\Scripts\python.exe" -m streamlit run app.py
set "launcher_exit_code=%ERRORLEVEL%"

echo.
if not "%launcher_exit_code%"=="0" (
    echo [ERROR] Streamlit exited with code %launcher_exit_code%.
) else (
    echo Streamlit stopped.
)
pause
exit /b %launcher_exit_code%
