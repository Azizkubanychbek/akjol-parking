@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Нужны права администратора (правила брандмауэра). Если их нет — перезапускаемся с запросом.
net session >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo === Установка: Парковка Акжол ===
python --version >nul 2>&1
if errorlevel 1 (
  echo Python не найден. Установите Python 3.8+ с python.org с галочкой "Add Python to PATH" и запустите снова.
  pause
  exit /b 1
)
python --version
python -c "import parking.core, parking.web, parking.wlsite, parking.telegram" || (echo Ошибка в файлах программы & pause & exit /b 1)

echo Брандмауэр: панель 8100 (только локальная сеть), сайт белого списка 8101...
netsh advfirewall firewall delete rule name="Akjol parking panel" >nul 2>&1
netsh advfirewall firewall add rule name="Akjol parking panel" dir=in action=allow protocol=TCP localport=8100 profile=private,domain >nul
netsh advfirewall firewall delete rule name="Akjol whitelist site" >nul 2>&1
netsh advfirewall firewall add rule name="Akjol whitelist site" dir=in action=allow protocol=TCP localport=8101 >nul

echo Ярлык на рабочем столе и автозапуск...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"

echo.
echo Готово. Запуск: ярлык "Парковка Акжол" на рабочем столе.
echo ВАЖНО: перед запуском остановите старую программу (Parking.Api, QR-сервер, сторож whitelist) — см. CLAUDE.md.
pause
