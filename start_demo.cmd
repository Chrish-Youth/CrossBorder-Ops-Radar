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

if not defined DEEPSEEK_API_KEY (
    for /f "usebackq delims=" %%K in (`powershell.exe -NoProfile -NonInteractive -Command "[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')"`) do set "DEEPSEEK_API_KEY=%%K"
)

if /i "%~1"=="--check" (
    if defined DEEPSEEK_API_KEY (
        echo [OK] DeepSeek API key is available to this process.
    ) else (
        echo [WARN] DeepSeek API key is unavailable.
    )
    echo [OK] Launcher check completed. Streamlit was not started.
    exit /b 0
)

if not defined DEEPSEEK_API_KEY (
    echo [WARN] DeepSeek API key is unavailable.
    echo [SETUP] Paste it once at the secure prompt below.
    echo         The key will not be displayed or written into this script.
    echo.
    powershell.exe -NoProfile -Command "$secureKey = Read-Host 'DeepSeek API key' -AsSecureString; $plainKey = [System.Net.NetworkCredential]::new('', $secureKey).Password; if ([string]::IsNullOrWhiteSpace($plainKey)) { exit 1 }; [Environment]::SetEnvironmentVariable('DEEPSEEK_API_KEY', $plainKey, 'User'); $plainKey = $null; $secureKey.Dispose()"
    if errorlevel 1 (
        echo.
        echo [ERROR] No API key was saved. Streamlit was not started.
        pause
        exit /b 1
    )
    for /f "usebackq delims=" %%K in (`powershell.exe -NoProfile -NonInteractive -Command "[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')"`) do set "DEEPSEEK_API_KEY=%%K"
)

if not defined DEEPSEEK_API_KEY (
    echo [ERROR] DeepSeek API key could not be loaded.
    pause
    exit /b 1
)

echo [OK] DeepSeek API key is available to this process.

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
