@echo off
title SquishBox Updater
cd /d C:\Users\brett\squishbox

echo Stopping SquishBox...
taskkill /F /FI "WINDOWTITLE eq SquishBox" >nul 2>&1
for /f "tokens=2" %%p in ('netstat -ano ^| findstr ":5555 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

echo Pulling latest code...
git pull

echo Starting SquishBox...
start "SquishBox" C:\Users\brett\venv\Scripts\python.exe app.py

echo.
echo Done! SquishBox is restarting.
timeout /t 3
