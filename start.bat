@echo off
chcp 65001 >nul
title Akjol parking (do not close)
cd /d "%~dp0"
:loop
python app.py
if errorlevel 3 if not errorlevel 4 exit /b
echo Программа остановилась, перезапуск через 5 секунд...
timeout /t 5 /nobreak >nul
goto loop
