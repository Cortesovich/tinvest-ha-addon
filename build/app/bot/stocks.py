"""Скоринг акций по фундаменталу T-Invest.

Скор = взвешенная сумма перцентильных рангов внутри списка голубых фишек:
Стоимость (доходность прибыли 1/PE) + Качество (ROE, долг/капитал) +
Рост (выручка) + Дивиденды + Личное мнение (правишь в stock_opinions.csv).
Плюс отдельно «премия к облигации» = доходность прибыли + часть роста − YTM бонда.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR
from .tinvest_client import StockFund

log = logging.getLogger("stocks")

OPINIONS_CSV = DATA_DIR / "stock_opinions.csv"
SCORES_CSV = DATA_DIR / "stocks_score.csv"


@dataclass
class StockScore:
    ticker: str
    name: str
    figi: str
    pe: float | None
    growth: float | None
    debt: float | None
    roe: float | None
    div: float | None
    value_s: float
    quality_s: float
    growth_s: float
    div_s: float
    personal: float
    total: float
    earnings_yield: float | None
    premium_vs_bond: float | None


def _percentiles(pairs, higher_better=True) -> dict:
    """Мидранг-перцентиль 0..100 для {key: value}."""
    vals = [v for _, v in pairs]
    n = len(vals)
    if n == 0:
        return {}
    if n == 1:
        return {pairs[0][0]: 100.0}
    out = {}
    for k, v in pairs:
        less = sum(1 for x in vals if x < v)
        equal = sum(1 for x in vals if x == v)
        r = (less + 0.5 * equal) / n * 100.0
        out[k] = r if higher_better else (100.0 - r)
    return out


DIV_CAP = 30.0  # дивдоходность выше — почти наверняка битые данные (ограничиваем для скора)


def score_stocks(funds: list[StockFund], opinions: dict, weights: dict,
                 bond_ytm: float | None, min_cap: float = 0.0,
                 min_ff: float = 0.0, min_pe: float = 1.0) -> list[StockScore]:
    universe = [
        f for f in funds
        if f.growth is not None and f.debt_to_equity is not None
        and (min_cap <= 0 or (f.market_cap or 0) >= min_cap)
        and (min_ff <= 0 or (f.free_float or 0) >= min_ff)
    ]
    if not universe:
        return []

    def valid_pe(f):
        return f.pe is not None and f.pe >= min_pe  # ниже min_pe = битые данные

    # Стоимость — только по бумагам с валидным P/E; остальным нейтрально (50)
    ey = {f.ticker: 100.0 / f.pe for f in universe if valid_pe(f)}
    p_value = _percentiles(list(ey.items()), True)
    p_growth = _percentiles([(f.ticker, f.growth) for f in universe], True)
    # Долг: 0 (банки) и None не ранжируем — им нейтрально
    p_debt = _percentiles([(f.ticker, f.debt_to_equity) for f in universe
                           if f.debt_to_equity not in (None, 0)], False)
    p_roe = _percentiles([(f.ticker, f.roe) for f in universe if f.roe is not None], True)
    p_div = _percentiles([(f.ticker, min(f.div_yield, DIV_CAP)) for f in universe
                          if f.div_yield is not None], True)

    out = []
    for f in universe:
        vs = p_value.get(f.ticker, 50.0)        # нет валидного P/E → нейтрально
        gs = p_growth[f.ticker]
        ds = p_debt.get(f.ticker, 50.0)         # долг 0/нет (банки) → нейтрально
        rs = p_roe.get(f.ticker, 50.0)
        qs = 0.5 * rs + 0.5 * ds
        dvs = p_div.get(f.ticker, 0.0)          # нет дивов → низкий балл
        pers = float(opinions.get(f.ticker, 100.0))
        total = (weights["value"] * vs + weights["quality"] * qs
                 + weights["growth"] * gs + weights["dividend"] * dvs
                 + weights["personal"] * pers) / 100.0
        eyield = ey.get(f.ticker)               # None, если P/E битый
        prem = (eyield - bond_ytm) if (eyield is not None and bond_ytm is not None) else None
        out.append(StockScore(
            ticker=f.ticker, name=f.name, figi=f.figi, pe=f.pe, growth=f.growth,
            debt=f.debt_to_equity, roe=f.roe, div=f.div_yield,
            value_s=vs, quality_s=qs, growth_s=gs, div_s=dvs, personal=pers,
            total=total, earnings_yield=eyield, premium_vs_bond=prem,
        ))
    out.sort(key=lambda s: s.total, reverse=True)
    return out


def load_opinions(funds: list[StockFund]) -> dict:
    """Читает stock_opinions.csv (создаёт/дополняет тикерами со 100 по умолчанию)."""
    rows: dict[str, float] = {}
    names: dict[str, str] = {f.ticker: f.name for f in funds}
    if OPINIONS_CSV.exists():
        try:
            with open(OPINIONS_CSV, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    t = (r.get("ticker") or "").upper()
                    if not t:
                        continue
                    names.setdefault(t, r.get("name") or t)
                    try:
                        rows[t] = float(r.get("opinion", 100))
                    except (TypeError, ValueError):
                        rows[t] = 100.0
        except Exception:  # noqa: BLE001
            log.exception("Не удалось прочитать stock_opinions.csv")

    added = False
    for f in funds:
        if f.ticker not in rows:
            rows[f.ticker] = 100.0
            added = True
    if added or not OPINIONS_CSV.exists():
        try:
            with open(OPINIONS_CSV, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["ticker", "name", "opinion"])
                for t in sorted(rows):
                    w.writerow([t, names.get(t, t), int(rows[t])])
        except Exception:  # noqa: BLE001
            log.exception("Не удалось записать stock_opinions.csv")
    return rows


def write_scores_csv(scores: list[StockScore]) -> Path:
    with open(SCORES_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "name", "PE", "growth", "debt_to_equity", "ROE",
                    "div_yield", "value_score", "quality_score", "growth_score",
                    "dividend_score", "personal", "TOTAL", "earnings_yield",
                    "premium_vs_bond"])
        for s in scores:
            w.writerow([
                s.ticker, s.name, _r(s.pe), _r(s.growth), _r(s.debt), _r(s.roe),
                _r(s.div), _r(s.value_s), _r(s.quality_s), _r(s.growth_s),
                _r(s.div_s), _r(s.personal), _r(s.total), _r(s.earnings_yield),
                _r(s.premium_vs_bond),
            ])
    return SCORES_CSV


def _r(x):
    return "" if x is None else round(float(x), 2)
