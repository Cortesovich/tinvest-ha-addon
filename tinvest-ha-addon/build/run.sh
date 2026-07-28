#!/usr/bin/with-contenv bashio
# Стартовый скрипт HA-дополнения.

bashio::log.info "TInvest ИИС Bot: запуск…"

# Папка данных на постоянном диске (переживает перезапуск/обновление дополнения).
mkdir -p /share/tinvest

if [ ! -f /share/tinvest/.env ]; then
  bashio::log.error "Нет файла /share/tinvest/.env с токенами."
  bashio::log.error "Создай его по инструкции и перезапусти дополнение."
  # Останавливаемся аккуратно, чтобы watchdog не крутил бесконечно на пустой конфиг.
  sleep 15
  exit 1
fi

cd /app
exec python3 main.py
