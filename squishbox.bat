@echo off
title SquishBox
cd /d C:\Users\brett\squishbox
:loop
C:\Users\brett\venv\Scripts\python.exe app.py
echo.
echo SquishBox stopped. Restarting in 3 seconds...
echo (Close this window to stop for real)
timeout /t 3
goto loop
