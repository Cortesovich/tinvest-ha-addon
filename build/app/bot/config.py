"""Загрузка конфигурации: секреты из .env, параметры из config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Папка данных: на ПК = корень проекта; в HA-дополнении = /share/tinvest
# (задаётся переменной окружения TINVEST_DATA_DIR). Тут лежат .env, config.yaml,
# stock_opinions.csv, stocks_score.csv, orders.log.
DATA_DIR = Path(os.getenv("TINVEST_DATA_DIR") or ROOT)

# Белый список эмитентов AAA/AA (утверждён 21.07.2026). Сопоставляется как
# подстрока с названием облигации (в нижнем регистре). ОФЗ допускаются всегда,
# отдельно. Переопределяется в config.yaml ключом bond_whitelist.
DEFAULT_BOND_WHITELIST = [
    "магнит", "ржд", "сбер", "втб", "газпромбанк", "россельхозбанк", "рсхб",
    "дом.рф", "дом рф", "вэб", "газпром", "роснефть", "лукойл", "новатэк",
    "транснефть", "норникель", "гмк", "полюс", "сибур", "фосагро",
    "ростелеком", "мтс", "икс 5", "x5", "россети", "фск",
    "атомэнергопром", "росатом",
]

# Голубые фишки МосБиржи — по ТИКЕРАМ (черновик, Роман вычитывает).
# Переопределяется в config.yaml ключом stock_whitelist.
DEFAULT_STOCK_WHITELIST = [
    "SBER", "SBERP", "GAZP", "LKOH", "GMKN", "ROSN", "NVTK", "PLZL",
    "TATN", "TATNP", "SNGS", "SNGSP", "SIBN", "TRNFP", "CHMF", "NLMK",
    "MAGN", "MGNT", "MTSS", "MOEX", "PHOR", "ALRS", "RUAL", "VTBR",
    "AFLT", "PIKK", "IRAO", "HYDR", "YDEX", "OZON", "T", "AFKS",
]


@dataclass
class Config:
    # --- секреты (.env) ---
    tinvest_token: str
    telegram_token: str
    trade_token: str = ""              # full-access токен для покупок (TINVEST_TRADE_TOKEN); пусто = использовать основной
    allowed_chat_ids: list[int] = field(default_factory=list)

    # --- параметры (config.yaml) ---
    account_type: str = "iis"          # какой счёт показывать: iis | broker | <account_id>
    coupon_lookahead_days: int = 180   # горизонт поиска ближайших купонов
    maturity_horizon_days: int = 1825  # горизонт для купонного графика (~5 лет)
    hide_zero_coupons: bool = True     # прятать купоны с ещё не определённой суммой (0)
    tax_refund_amounts: list = field(default_factory=list)  # пополнения-вычеты (считать доходом)
    timezone: str = "Europe/Moscow"
    poll_timeout: int = 30             # long-polling getUpdates, сек

    # --- отбор облигаций для реинвеста (Этап 3) ---
    bond_min_maturity_days: int = 183   # не покупать бумаги, гасящиеся раньше ~6 мес
    bond_max_maturity_days: int = 1825  # максимум 5 лет до погашения
    bond_top_n: int = 10                # кандидатов в плане покупки (больше = шире диверсификация)
    reinvest_min_cash: float = 1000.0   # ниже этой суммы свободных рублей — не предлагать
    bond_max_screened: int = 80         # предохранитель по числу расчётов YTM (rate limit)
    bond_whitelist: list = field(default_factory=lambda: list(DEFAULT_BOND_WHITELIST))
    # P2 — ПОРТФЕЛЬНЫЙ лимит концентрации: по одной КОМПАНИИ (корп. эмитенту)
    # не более X% облигационной части (с учётом уже купленного). ОФЗ без лимита.
    # 0 = выключен.
    bond_max_issuer_pct: float = 20.0

    # --- покупка по кнопке (Этап C) ---
    trade_enabled: bool = False         # ГЛАВНЫЙ выключатель покупок. False = кнопки нет
    reinvest_max_rub: float = 0.0       # лимит суммы на одну операцию, ₽ (0 = без доп. лимита)
    # P1 — лимитные заявки: цена = последняя цена + буфер (%). Буфер даёт запас,
    # чтобы заявка исполнилась, и одновременно задаёт ПОТОЛОК проскальзывания
    # (у рыночной заявки его нет). 0 = ставить ровно по последней цене.
    limit_buffer_pct: float = 0.5

    # --- топ-листы и отбор акций ---
    topbonds_n: int = 5                 # сколько облигаций показывать в /topbonds
    stock_top_n: int = 10               # сколько акций показывать в /topstocks
    stock_min_market_cap: float = 0.0   # мин. капитализация (0 = не фильтровать)
    stock_min_free_float: float = 0.0   # мин. free-float, доля/проц (0 = не фильтровать)
    stock_min_pe: float = 1.0           # P/E ниже — считаем битыми данными (нейтральная оценка)
    w_value: float = 22.5               # веса скора акции (в сумме 100)
    w_quality: float = 22.5
    w_growth: float = 20.0
    w_dividend: float = 15.0
    w_personal: float = 20.0
    stock_whitelist: list = field(default_factory=lambda: list(DEFAULT_STOCK_WHITELIST))

    # --- второй пользователь: только чтение (без покупок) ---
    viewer_chat_ids: list = field(default_factory=list)   # chat_id-ы «наблюдателей»

    # --- автопокупка облигаций (Этап D) ---
    autobuy_enabled: bool = False        # главный выключатель автопокупки
    autobuy_min_cash: float = 1200.0     # покупать, только если свободно >= этой суммы, ₽
    autobuy_days: list = field(default_factory=lambda: ["mon", "fri"])  # дни недели
    autobuy_time: str = "11:00"          # время запуска в часовом поясе timezone (ЧЧ:ММ)


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise SystemExit(
            f"Не задана переменная окружения {name}. "
            f"Скопируй .env.example в .env и заполни."
        )
    return val


def load_config() -> Config:
    load_dotenv(DATA_DIR / ".env")

    # chat_id-ы через запятую: TELEGRAM_ALLOWED_CHAT_IDS=123,456
    raw_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    allowed = [int(x) for x in raw_ids.replace(" ", "").split(",") if x]

    params: dict = {}
    cfg_path = DATA_DIR / "config.yaml"
    if cfg_path.exists():
        params = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    return Config(
        tinvest_token=_require("TINVEST_TOKEN"),
        telegram_token=_require("TELEGRAM_TOKEN"),
        trade_token=os.getenv("TINVEST_TRADE_TOKEN", "").strip(),
        allowed_chat_ids=allowed,
        account_type=str(params.get("account_type", "iis")),
        coupon_lookahead_days=int(params.get("coupon_lookahead_days", 180)),
        maturity_horizon_days=int(params.get("maturity_horizon_days", 1825)),
        hide_zero_coupons=bool(params.get("hide_zero_coupons", True)),
        tax_refund_amounts=[float(x) for x in (params.get("tax_refund_amounts") or [])],
        timezone=str(params.get("timezone", "Europe/Moscow")),
        poll_timeout=int(params.get("poll_timeout", 30)),
        bond_min_maturity_days=int(params.get("bond_min_maturity_days", 183)),
        bond_max_maturity_days=int(params.get("bond_max_maturity_days", 1825)),
        bond_top_n=int(params.get("bond_top_n", 10)),
        reinvest_min_cash=float(params.get("reinvest_min_cash", 1000)),
        bond_max_screened=int(params.get("bond_max_screened", 80)),
        bond_whitelist=[str(x).lower() for x in
                        (params.get("bond_whitelist") or DEFAULT_BOND_WHITELIST)],
        bond_max_issuer_pct=float(params.get("bond_max_issuer_pct", 20)),
        trade_enabled=bool(params.get("trade_enabled", False)),
        reinvest_max_rub=float(params.get("reinvest_max_rub", 0)),
        limit_buffer_pct=float(params.get("limit_buffer_pct", 0.5)),
        topbonds_n=int(params.get("topbonds_n", 5)),
        stock_top_n=int(params.get("stock_top_n", 10)),
        stock_min_market_cap=float(params.get("stock_min_market_cap", 0)),
        stock_min_free_float=float(params.get("stock_min_free_float", 0)),
        stock_min_pe=float(params.get("stock_min_pe", 1.0)),
        w_value=float(params.get("w_value", 22.5)),
        w_quality=float(params.get("w_quality", 22.5)),
        w_growth=float(params.get("w_growth", 20)),
        w_dividend=float(params.get("w_dividend", 15)),
        w_personal=float(params.get("w_personal", 20)),
        stock_whitelist=[str(x).upper() for x in
                         (params.get("stock_whitelist") or DEFAULT_STOCK_WHITELIST)],
        viewer_chat_ids=[int(x) for x in (params.get("viewer_chat_ids") or [])],
        autobuy_enabled=bool(params.get("autobuy_enabled", False)),
        autobuy_min_cash=float(params.get("autobuy_min_cash", 1200)),
        autobuy_days=[str(x).strip().lower() for x in
                      (params.get("autobuy_days") or ["mon", "fri"])],
        autobuy_time=str(params.get("autobuy_time", "11:00")),
    )
