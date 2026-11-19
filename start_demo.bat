@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Colab Boost Console] Starting Sandbox Demo Mode (0 Billing) on http://127.0.0.1:8765 ...
uv run colab-boost --mock --mock-scenario demo --open-browser
