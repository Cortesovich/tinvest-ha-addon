# Базовый образ передаётся из workflow (per-arch), значение по умолчанию — aarch64.
ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.23
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TINVEST_DATA_DIR=/share/tinvest

WORKDIR /app

# Зависимости отдельным слоем. build-base + yaml-dev нужны только на время
# сборки (если для musl/arch нет готового wheel, например PyYAML соберётся
# из исходников). После установки инструменты удаляем — образ остаётся лёгким.
COPY app/requirements.txt /app/requirements.txt
RUN apk add --no-cache --virtual .build-deps build-base yaml-dev \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && apk del .build-deps

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Предохранитель: битый код — сборка падает сразу.
RUN python -m py_compile /app/main.py /app/bot/*.py

CMD [ "/run.sh" ]
