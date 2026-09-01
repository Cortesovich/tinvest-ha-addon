# Базовый образ передаётся из workflow (per-arch), значение по умолчанию — aarch64.
# Пин явный: не полагаемся на авто-подстановку BUILD_FROM Supervisor'ом.
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.23
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCORING_HOME=/opt/scoring \
    SCORING_VENV=/opt/scoring-venv \
    SCORING_METHODOLOGY=/opt/scoring/methodology-v0.3.md

# tzdata — расписание в Europe/Moscow (zoneinfo); ca-certificates — HTTPS к
# MOEX ISS и к приёмнику витрины. Токена/сертификата Т-Банка здесь НЕТ:
# аддон в Т-Банк не ходит.
RUN apk add --no-cache tzdata ca-certificates

WORKDIR /opt/scoring

# Изолированный venv + установка ТОЛЬКО локального wheel: без сети (--no-index)
# и без внешних зависимостей (--no-deps; пакет на stdlib). Проверка версии на
# этапе сборки — если пакет не встал, образ падает сразу.
COPY wheels/tinvest_scoring_proto-0.3.1-py3-none-any.whl /opt/scoring/wheels/
RUN python3 -m venv "$SCORING_VENV" \
    && "$SCORING_VENV/bin/python" -m pip install --no-index --no-deps \
        /opt/scoring/wheels/tinvest_scoring_proto-0.3.1-py3-none-any.whl \
    && "$SCORING_VENV/bin/python" -m tinvest_scoring --version

# Неизменённая методология — обязательный вход market-refresh.
COPY methodology-v0.3.md /opt/scoring/methodology-v0.3.md

# Обёртка: планировщик + чтение /share + запуск CLI пакета + валидация +
# публикация. Запускается системным python3 базового образа; расчётный CLI —
# из venv. Предохранитель py_compile на этапе сборки.
COPY app/ /opt/scoring/app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh \
    && python3 -m py_compile /opt/scoring/app/scoring_runner.py

CMD [ "/run.sh" ]
