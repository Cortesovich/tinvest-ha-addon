"""Точка входа ИИС-бота.

Запуск:
    python main.py           — запустить Telegram-бота (long-polling)
    python main.py --check   — проверить токен и подключение к T-Invest API
    python main.py --accounts — вывести список доступных счетов
    python main.py --export  — read-only выгрузка снимков (portfolio/allowlist/fundamentals)
    python main.py --push    — разово отправить снимок портфеля в приёмник Mini App
"""
from __future__ import annotations

import logging
import sys

from bot import __version__
from bot.config import load_config
from bot.telegram_bot import TelegramBot
from bot.tinvest_client import TInvestClient


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    setup_logging()
    log = logging.getLogger("main")
    cfg = load_config()
    tinvest = TInvestClient(cfg.tinvest_token, cfg.account_type, cfg.trade_token)

    if "--check" in sys.argv:
        log.info("ИИС-бот v%s — проверка подключения…", __version__)
        print(tinvest.check())
        return

    if "--accounts" in sys.argv:
        for a in tinvest.list_accounts():
            print(a)
        return

    if "--export" in sys.argv:
        from bot import snapshot_export
        log.info("ИИС-бот v%s — read-only экспорт снимков…", __version__)
        res = snapshot_export.run_export(
            tinvest,
            export_dir=cfg.export_dir or None,
            coupon_lookahead_days=cfg.coupon_lookahead_days,
            fundamentals_scope=cfg.export_fundamentals_scope,
            stock_whitelist=cfg.stock_whitelist,
        )
        for a in res.artifacts:
            print(f"{a.name}: {a.status} (строк {a.rows}) {a.error}".rstrip())
        print(f"Папка: {res.dir}")
        return

    if "--push" in sys.argv:
        from bot import snapshot_export
        if not (cfg.export_push_enabled and cfg.export_push_url
                and cfg.export_push_secret):
            print("Push выключен/не настроен: нужны export_push_enabled + "
                  "export_push_url (config.yaml) и EXPORT_PUSH_SECRET (.env).")
            return
        res, _ = snapshot_export.export_and_push_portfolio(
            tinvest, url=cfg.export_push_url, secret=cfg.export_push_secret,
            export_dir=cfg.export_dir or None,
            coupon_lookahead_days=cfg.coupon_lookahead_days)
        print(f"push: {res.status} ({res.code}) {res.message}")
        return

    log.info("ИИС-бот v%s — старт", __version__)
    TelegramBot(cfg, tinvest).run()


if __name__ == "__main__":
    main()
