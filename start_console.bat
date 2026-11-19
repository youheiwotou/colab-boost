@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Colab Boost Console] Starting server on http://127.0.0.1:8765 ...
uv run colab-boost --open-browser %*
