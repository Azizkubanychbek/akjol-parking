@echo off
rem Экран оплаты на 2-м мониторе. POS — координаты левого верхнего угла 2-го монитора
rem (если 2-й монитор справа от основного 1920 px — 1920,0).
set URL=http://127.0.0.1:8100/screen
set POS=1920,0
set PROFILE=%LOCALAPPDATA%\akjol_screen
set CHROME=
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set CHROME=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe
if defined CHROME (
  start "" "%CHROME%" --kiosk --new-window --window-position=%POS% --user-data-dir="%PROFILE%" --no-first-run %URL%
) else (
  start "" msedge --kiosk %URL% --edge-kiosk-type=fullscreen --window-position=%POS% --user-data-dir="%PROFILE%" --no-first-run
)
