# TInvest ИИС Bot — Home Assistant add-on

Локальный Telegram-бот для ИИС в Т-Инвестициях, упакованный как дополнение
Home Assistant. Образ собирается на GitHub Actions и публикуется на `ghcr.io`,
поэтому сервер (даже с блокировкой Docker Hub) просто **скачивает готовый образ**.

## Как пользоваться

1. Форкни/создай репозиторий с этими файлами, включи GitHub Actions.
2. В `tinvest_bot/config.yaml` укажи свой логин GitHub (нижний регистр) в поле `image`.
3. Дождись сборки (вкладка **Actions**) и сделай пакеты `tinvest-bot-*` публичными.
4. В Home Assistant добавь этот репозиторий: **Settings → Add-ons → Store → ⋮ →
   Repositories**, затем установи «TInvest ИИС Bot».
5. Токены и параметры положи в `/share/tinvest/.env` и `/share/tinvest/config.yaml`.

Секреты (токены T-Invest и Telegram) в репозитории **не хранятся** — только на сервере
в `/share/tinvest`.

## Что где

- `build/` — контекст сборки образа (Dockerfile, код бота `app/`, `run.sh`).
- `tinvest_bot/` — манифест дополнения (использует готовый образ с ghcr.io).
- `.github/workflows/build.yml` — сборка образа под aarch64 и amd64 + публикация.
