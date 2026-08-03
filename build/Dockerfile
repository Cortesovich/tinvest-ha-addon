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

# --- Российский Национальный УЦ (Минцифры) ---
# С 2025 API Т-Банка (invest-public-api.tbank.ru) подписан сертификатом
# НУЦ Минцифры РФ, которого нет в стандартном наборе certifi. Собираем общий
# бандл = стандартные корни (certifi) + российские (root+sub) и указываем его
# для requests/urllib через REQUESTS_CA_BUNDLE / SSL_CERT_FILE.
COPY russian_trusted.pem /usr/local/share/ca-certificates/russian_trusted.pem
RUN cat "$(python -c 'import certifi; print(certifi.where())')" \
        /usr/local/share/ca-certificates/russian_trusted.pem \
        > /etc/ssl/certs/ca-combined.pem
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-combined.pem \
    SSL_CERT_FILE=/etc/ssl/certs/ca-combined.pem

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Предохранитель: если код не компилируется — сборка падает сразу (а не при запуске).
RUN python -m py_compile /app/main.py /app/bot/*.py

CMD [ "/run.sh" ]
