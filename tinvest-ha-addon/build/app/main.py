"""Точка входа ИИС-бота.

Запуск:
    python main.py           — запустить Telegram-бота (long-polling)
    python main.py --check   — проверить токен и подключение к T-Invest API
    python main.py --accounts — вывести список доступных счетов
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

    log.info("ИИС-бот v%s — старт", __version__)
    TelegramBot(cfg, tinvest).run()


if __name__ == "__main__":
    main()
