"""Оффлайн-тесты read-only экспортёра снимков (без сети, без брокера).

Проверяют НОВЫЙ код: сборку/маппинг, market_value из текущей цены (не средней),
атомарную запись, детерминизм JSON, политику stale/not_connected, скруббер
секретов и отсутствие любых торговых вызовов.

Запуск:  cd build/app && python -m pytest ../../tests -q
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# путь к пакету bot (build/app)
APP_DIR = Path(__file__).resolve().parents[1] / "build" / "app"
sys.path.insert(0, str(APP_DIR))

from bot import snapshot_export as se           # noqa: E402
from bot.tinvest_client import CouponItem, TInvestClient, TInvestError  # noqa: E402

ACC_ID = "ACC-SECRET-123"
ACC_NAME = "ИИС-SECRET"


def _mv(units, nano=0, currency="rub"):
    return {"units": str(units), "nano": nano, "currency": currency}


class FakeClient:
    """Только read-only методы, которые использует snapshot_export.
    Торговых методов нет намеренно — их вызов вызвал бы AttributeError."""

    def __init__(self, fail_portfolio=False):
        self.fail_portfolio = fail_portfolio

    def resolve_account(self):
        return ACC_ID, ACC_NAME

    def portfolio_raw(self):
        if self.fail_portfolio:
            raise TInvestError("нет связи с API")
        return {
            "totalAmountPortfolio": _mv(200000),
            "totalAmountCurrencies": _mv(12500),
            "positions": [
                {"figi": "BOND1", "instrumentType": "bond",
                 "quantity": _mv(12), "currentPrice": _mv(1015)},
                {"figi": "SHARE1", "instrumentType": "share",
                 "quantity": _mv(10), "currentPrice": _mv(250)},
                {"figi": "CUR", "instrumentType": "currency",
                 "quantity": _mv(1)},   # не позиция инструмента — пропускается
            ],
        }

    def _last_prices(self, figis):
        return {"BOND1": Decimal("101.5")}

    def _bond_info(self, figi):
        return {"name": "Обл A", "ticker": "RU000A1",
                "nominal": _mv(1000), "aciValue": _mv(5)}

    def instrument_brief(self, figi):
        return {"name": "Акция X", "ticker": "SBER"}

    def get_upcoming_coupons(self, days, hide_zero=True):
        assert hide_zero is True   # плавающие купоны должны отсекаться источником
        return [CouponItem(bond_name="Обл A", ticker="RU000A1",
                           date=datetime(2026, 7, 15, tzinfo=timezone.utc),
                           per_bond=Decimal("10"), quantity=Decimal("12"),
                           total=Decimal("120"), currency="RUB")]

    def iis_universe_rows(self):
        return [
            {"secid": "SBER", "isin": "RU0009029540", "figi": "F1",
             "instrument_type": "share", "currency": "rub",
             "api_trade_available": True, "buy_available": True,
             "sell_available": None},                      # флаг не отдан → пусто
            {"secid": "RU000A1", "isin": "RU000A1", "figi": "F2",
             "instrument_type": "bond", "currency": "rub",
             "api_trade_available": True, "buy_available": False,
             "sell_available": True},
        ]

    def raw_fundamentals(self, tickers):
        return [{"secid": "SBER", "isin": "RU0009029540",
                 "api_field": "roe", "api_value": 0.24}]


# ------------------------------- портфель ---------------------------------

def test_portfolio_snapshot_shape_and_values():
    snap = se.build_portfolio_snapshot(FakeClient())
    assert set(snap) == {"as_of", "cash_value", "currency", "payments",
                         "positions", "source"}
    assert snap["cash_value"] == 12500
    # market_value из ТЕКУЩЕЙ цены: облигация 12*(101.5%*1000+НКД5)=12240; акция 10*250
    bond = next(p for p in snap["positions"] if p["instrument_type"] == "bond")
    share = next(p for p in snap["positions"] if p["instrument_type"] == "share")
    assert bond["market_value"] == 12240 and bond["secid"] == "RU000A1"
    assert share["market_value"] == 2500 and share["secid"] == "SBER"
    # позиции инструментов ровно две (валюта отброшена)
    assert len(snap["positions"]) == 2
    # купон смаппился
    assert snap["payments"][0] == {"amount": 120, "currency": "RUB",
                                   "kind": "coupon", "name": "Обл A",
                                   "payment_at": "2026-07-15T00:00:00Z",
                                   "secid": "RU000A1"}
    # никаких запрещённых полей
    assert "average" not in json.dumps(snap).lower()
    for banned in ("account", "figi", "uid", ACC_ID, ACC_NAME):
        assert banned not in json.dumps(snap, ensure_ascii=False)


# --------------------------- полный экспорт --------------------------------

def test_run_export_writes_all_three(tmp_path):
    res = se.run_export(FakeClient(), export_dir=str(tmp_path),
                        fundamentals_scope="whitelist", stock_whitelist=["SBER"])
    assert {a.name: a.status for a in res.artifacts} == {
        "portfolio": "ok", "allowlist": "ok", "fundamentals": "ok"}
    # три файла + статус
    for fn in (se.PORTFOLIO_JSON, se.ALLOWLIST_CSV, se.FUNDAMENTALS_CSV,
               se.STATUS_JSON):
        assert (tmp_path / fn).exists()
    # JSON детерминирован: sort_keys + перевод строки в конце
    txt = (tmp_path / se.PORTFOLIO_JSON).read_text(encoding="utf-8")
    assert txt.endswith("\n")
    assert json.loads(txt) == json.loads(
        json.dumps(json.loads(txt), ensure_ascii=False, sort_keys=True))
    # allowlist: отсутствующий sell_available → пустая ячейка
    csv_lines = (tmp_path / se.ALLOWLIST_CSV).read_text(encoding="utf-8").splitlines()
    sber = [ln for ln in csv_lines if ln.startswith("") and "SBER" in ln][0]
    assert sber.split(",")[6:9] == ["true", "true", ""]  # trade, buy, sell(пусто)
    status = json.loads((tmp_path / se.STATUS_JSON).read_text(encoding="utf-8"))
    assert status["portfolio"]["status"] == "ok"


def test_error_does_not_clobber_good_file(tmp_path):
    # первый успешный прогон
    se.run_export(FakeClient(), export_dir=str(tmp_path), stock_whitelist=["SBER"])
    good = (tmp_path / se.PORTFOLIO_JSON).read_text(encoding="utf-8")
    # второй прогон со сбоем портфеля — файл НЕ перезаписан, статус stale
    res = se.run_export(FakeClient(fail_portfolio=True), export_dir=str(tmp_path),
                        stock_whitelist=["SBER"])
    pf = next(a for a in res.artifacts if a.name == "portfolio")
    assert pf.status == "error"
    assert (tmp_path / se.PORTFOLIO_JSON).read_text(encoding="utf-8") == good
    status = json.loads((tmp_path / se.STATUS_JSON).read_text(encoding="utf-8"))
    assert status["portfolio"]["status"] == "stale"           # был успех раньше
    assert status["allowlist"]["status"] == "ok"              # остальные не задеты


def test_not_connected_when_never_succeeded(tmp_path):
    res = se.run_export(FakeClient(fail_portfolio=True), export_dir=str(tmp_path),
                        stock_whitelist=["SBER"])
    status = json.loads((tmp_path / se.STATUS_JSON).read_text(encoding="utf-8"))
    assert status["portfolio"]["status"] == "not_connected"
    assert not (tmp_path / se.PORTFOLIO_JSON).exists()


def test_secret_scrubber_blocks_leak():
    with pytest.raises(se.ExportError):
        se._assert_no_secrets(f'{{"x":"{ACC_ID}"}}', [ACC_ID, ACC_NAME])
    se._assert_no_secrets('{"x":"ok"}', [ACC_ID, ACC_NAME])   # чисто — не бросает


def test_fundamentals_scope_none_writes_header_only(tmp_path):
    se.run_export(FakeClient(), export_dir=str(tmp_path),
                  fundamentals_scope="none", stock_whitelist=["SBER"])
    lines = (tmp_path / se.FUNDAMENTALS_CSV).read_text(encoding="utf-8").splitlines()
    assert lines == [",".join(se._FUND_HEADER)]               # только заголовок


# --------------------- read-only методы клиента ----------------------------

def test_iis_universe_filters_for_iis_and_flags():
    c = TInvestClient.__new__(TInvestClient)   # без сети
    c._all_bonds = lambda: [
        {"ticker": "B1", "isin": "I1", "figi": "F1", "currency": "rub",
         "forIisFlag": True, "apiTradeAvailableFlag": True,
         "buyAvailableFlag": True, "sellAvailableFlag": True},
        {"ticker": "B2", "figi": "F2", "forIisFlag": False},   # не ИИС → выкинуть
    ]
    c._all_shares = lambda: [
        {"ticker": "S1", "isin": "I3", "figi": "F3", "currency": "rub",
         "forIisFlag": True, "apiTradeAvailableFlag": True,
         "buyAvailableFlag": True},                            # sell не отдан
    ]
    rows = c.iis_universe_rows()
    assert [r["secid"] for r in rows] == ["B1", "S1"]
    assert rows[1]["sell_available"] is None                   # отсутствует → None


def test_raw_fundamentals_skips_null_and_nested():
    c = TInvestClient.__new__(TInvestClient)
    c._all_shares = lambda: [
        {"ticker": "SBER", "isin": "RU0009029540", "assetUid": "A1",
         "currency": "rub"}]
    c._call = lambda service, method, payload: {
        "fundamentals": [{"assetUid": "A1", "roe": 0.24, "pe": None,
                          "nested": {"x": 1}, "currency": "rub"}]}
    rows = c.raw_fundamentals(["SBER"])
    fields = {r["api_field"] for r in rows}
    assert fields == {"roe"}                    # null(pe)/nested/currency отброшены
    assert rows[0]["api_value"] == 0.24


# ------------------------- запрет торговых вызовов -------------------------

def test_module_has_no_order_calls():
    # AST, а не текст: ловим реальные вызовы, а не упоминания в докстроке.
    tree = ast.parse((APP_DIR / "bot" / "snapshot_export.py").read_text("utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in (
                "post_limit_buy", "post_market_buy", "post_order"):
            bad.append(node.attr)
        if (isinstance(node, ast.keyword) and node.arg == "trade"
                and isinstance(node.value, ast.Constant)
                and node.value.value is True):
            bad.append("trade=True")
        if isinstance(node, ast.Call):
            for a in node.args:
                if isinstance(a, ast.Constant) and a.value in (
                        "PostOrder", "CancelOrder"):
                    bad.append(a.value)
    assert not bad, f"торговые вызовы в экспортёре: {bad}"


def test_export_makes_no_trade_calls(tmp_path):
    # FakeClient не имеет торговых методов: успешный прогон = ни одного их вызова
    res = se.run_export(FakeClient(), export_dir=str(tmp_path),
                        stock_whitelist=["SBER"])
    assert all(a.status == "ok" for a in res.artifacts)
