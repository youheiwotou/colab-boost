@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>&1
if errorlevel 1 (
    echo [Colab Boost Console] Error: 'uv' was not found in PATH.
    echo Please install uv first: https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

echo [Colab Boost Console] Starting Sandbox Demo Mode (0 Billing) on http://127.0.0.1:8765 ...
uv run colab-boost --mock --mock-scenario demo --open-browser %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [Colab Boost Console] Exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
