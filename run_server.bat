@echo off
setlocal
cd /d "%~dp0"
title Xiaozhi AI Server - Ultra Low Latency

echo ========================================================
echo   KHOI DONG XIAOZHI AI ULTRA-LOW-LATENCY SERVER
echo   Online Search, Music Streaming, Natural Voice
echo ========================================================
echo.

echo [1/3] Dang kiem tra thu vien Python...
python -m pip install -r requirements.txt

echo.
echo [2/3] Kiem tra va giai phong cong 8000 neu dang bi chiem...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo Phat hien tien trinh cu PID %%a dang chiem cong 8000, dang dong...
    taskkill /F /PID %%a >nul 2>&1
)

echo.
echo [3/3] Dang khoi dong Server AI tai cong 8000...
echo.
python server.py

pause
