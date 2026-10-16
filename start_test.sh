#!/bin/sh
# Тестовый запуск на Mac: камеры выключены, номера вводятся в панели (вкладка «Тариф и тест»)
cd "$(dirname "$0")"
open http://127.0.0.1:8100/ http://127.0.0.1:8100/screen http://127.0.0.1:8101/ 2>/dev/null &
exec python3 app.py config.test.json
