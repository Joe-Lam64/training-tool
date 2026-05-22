@echo off
chcp 65001 >nul
title External Training Management System

echo ================================================================
echo   Loading External Training Management System
echo ================================================================

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found! Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import flask, requests, bs4, lxml" >nul 2>&1
if errorlevel 1 (
    echo Installing packages, wait 1-2 min...
    python -m pip install --user flask requests beautifulsoup4 lxml gunicorn
    if errorlevel 1 ( echo [ERROR] Package install failed && pause && exit /b 1 )
)

echo.
echo Starting server...
echo.
python app.py
pause
