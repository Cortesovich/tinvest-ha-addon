#!/usr/bin/with-contenv bashio
# Стартовый скрипт отдельного скоринг-аддона (read-only).

bashio::log.info "TInvest Скоринг: запуск отдельного read-only процесса…"

# Настройки из options.json → окружение. Массив retry_times склеиваем в CSV.
export PUBLISH_ENABLED="$(bashio::config 'publish_enabled')"
export RUN_ON_START="$(bashio::config 'run_on_start')"
export SCORING_INGEST_URL="$(bashio::config 'scoring_ingest_url')"
export SCHEDULE_TIME="$(bashio::config 'schedule_time')"
export SCORING_TZ="$(bashio::config 'timezone')"
export WORKERS="$(bashio::config 'workers')"
export INPUT_MAX_AGE_HOURS="$(bashio::config 'input_max_age_hours')"
export USE_QUALITY_SNAPSHOT="$(bashio::config 'use_quality_snapshot')"
export ATTEMPT_TIMEOUT_MINUTES="$(bashio::config 'attempt_timeout_minutes')"
export LOG_LEVEL="$(bashio::config 'log_level')"
export RETRY_TIMES="$(bashio::config 'retry_times | join(",")')"

# Секрет — в отдельную переменную. НЕ логируем его и НЕ передаём дочернему
# расчётному процессу (обёртка вычищает его из среды subprocess).
SCORING_SERVICE_SECRET="$(bashio::config 'scoring_service_secret')"
export SCORING_SERVICE_SECRET

if bashio::var.true "${PUBLISH_ENABLED}" && [ -z "${SCORING_SERVICE_SECRET}" ]; then
  bashio::log.warning "publish_enabled=true, но scoring_service_secret пуст — публикация будет пропущена."
fi

# Постоянное состояние аддона (переживает перезапуск/обновление).
mkdir -p /data/scoring/runs

cd /opt/scoring/app
exec python3 scoring_runner.py
