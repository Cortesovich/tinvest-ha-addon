"""Оффлайн-тесты push v2 + матрицы статусов/повторов (без сети).

v2: schema_version, performance из /income, монотонный as_of, строгий набор
полей, разбор ответа приёмника (202/200/409/422). Секрет/суммы в лог не идут.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import requests

APP_DIR = Path(__file__).resolve().parents[1] / "build" / "app"
sys.path.insert(0, str(APP_DIR))

from bot import snapshot_export as se                    # noqa: E402
from bot.tinvest_client import CouponItem, ReturnInfo    # noqa: E402


def _nosleep(_s):
    return None


class FakeResp:
    def __init__(self, code, body=None, text=""):
        self.status_code = code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakePost:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        item = self.seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch(monkeypatch, seq):
    fp = FakePost(seq)
    monkeypatch.setattr(se.requests, "post", fp)
    return fp


# ---------------------- push_snapshot: статусы/повторы ----------------------

def test_accepted_202(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(202, {"status": "accepted"})])
    res = se.push_snapshot("http://x/ingest", "SEC", '{"a":1}', sleep=_nosleep)
    assert (res.status, res.code, res.result) == ("accepted", 202, "accepted")
    assert len(fp.calls) == 1


def test_unchanged_200(monkeypatch):
    _patch(monkeypatch, [FakeResp(200, {"status": "unchanged"})])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_nosleep)
    assert (res.status, res.code) == ("unchanged", 200)


@pytest.mark.parametrize("code,body", [
    (401, {"error": "unauthorized"}),
    (409, {"error": "stale_snapshot"}),
    (409, {"error": "snapshot_timestamp_conflict"}),
    (413, {}), (415, {}), (422, {"error": "invalid_snapshot_contract"})])
def test_no_retry_codes(monkeypatch, code, body):
    fp = _patch(monkeypatch, [FakeResp(code, body)])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_nosleep)
    assert res.status == "error" and res.code == code
    assert len(fp.calls) == 1                      # НЕ повторяли
    if body.get("error"):
        assert res.result == body["error"]         # причина из тела


def test_retry_429_then_accepted(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(429, {}), FakeResp(202, {"status": "accepted"})])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_nosleep)
    assert res.status == "accepted" and len(fp.calls) == 2


def test_5xx_exhausts(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(503), FakeResp(500), FakeResp(502)])
    res = se.push_snapshot("http://x", "SEC", "{}", attempts=3, sleep=_nosleep)
    assert res.status == "error" and res.code == 502 and len(fp.calls) == 3


def test_network_then_accepted(monkeypatch):
    fp = _patch(monkeypatch, [requests.ConnectionError("boom"),
                              FakeResp(202, {"status": "accepted"})])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_nosleep)
    assert res.status == "accepted" and len(fp.calls) == 2


def test_network_exhausts(monkeypatch):
    fp = _patch(monkeypatch, [requests.ConnectionError("a"), requests.Timeout("b"),
                              requests.ConnectionError("c")])
    res = se.push_snapshot("http://x", "SEC", "{}", attempts=3, sleep=_nosleep)
    assert res.status == "network" and len(fp.calls) == 3


def test_headers_and_raw_body(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(202, {"status": "accepted"})])
    se.push_snapshot("http://x/ingest", "TOPSECRET", '{"k":"v"}',
                     as_of="2026-08-26T18:30:00Z", sleep=_nosleep)
    h = fp.calls[0]["headers"]
    assert h["Authorization"] == "Bearer TOPSECRET"
    assert h["Content-Type"] == "application/json"
    assert fp.calls[0]["data"] == b'{"k":"v"}'        # голое тело, без обёртки


# ---------------------- монотонный as_of ----------------------

def test_next_push_as_of_monotonic(tmp_path):
    a = se._next_push_as_of(tmp_path)
    b = se._next_push_as_of(tmp_path)
    assert b > a                       # строго растёт даже в пределах секунды


def test_next_push_as_of_bumps_from_future(tmp_path):
    (tmp_path / se.ASOF_STATE).write_text("2099-01-01T00:00:00Z", encoding="utf-8")
    assert se._next_push_as_of(tmp_path) == "2099-01-01T00:00:01Z"   # предыдущий + 1с


# ---------------------- build_snapshot_v2 ----------------------

def _mv(u, n=0, c="rub"):
    return {"units": str(u), "nano": n, "currency": c}


class FakeClientV2:
    def __init__(self, xirr=27.57):
        self._xirr = xirr

    def resolve_account(self):
        return "ACC-SECRET", "ИИС"

    def portfolio_raw(self):
        return {"totalAmountPortfolio": _mv(300000),
                "totalAmountCurrencies": _mv(387, 860000000),
                "positions": [{"figi": "B1", "instrumentType": "bond",
                               "quantity": _mv(10), "currentPrice": _mv(1012)}]}

    def _last_prices(self, figis):
        return {"B1": Decimal("101.0")}

    def _bond_info(self, figi):
        return {"name": "РЖД БО 001P-36R", "ticker": "RU000A1",
                "nominal": _mv(1000), "aciValue": _mv(12)}

    def instrument_brief(self, figi):
        return {}

    def get_upcoming_coupons(self, days, hide_zero=True):
        return [CouponItem(bond_name="РЖД БО 001P-36R", ticker="RU000A1",
                           date=datetime(2026, 9, 15, tzinfo=timezone.utc),
                           per_bond=Decimal("9.34"), quantity=Decimal("10"),
                           total=Decimal("93.4"), currency="RUB")]

    def compute_return(self, refunds):
        return ReturnInfo(
            account_name="ИИС", nav=Decimal("336722"),
            contributed=Decimal("277000"), withdrawn=Decimal("0"),
            coupons=Decimal("42715.34"), dividends=Decimal("0"),
            taxes=Decimal("0"), tax_refund=Decimal("26000"),
            profit=Decimal("59722"), xirr_pct=self._xirr,
            since=datetime(2025, 8, 4, tzinfo=timezone.utc), currency="RUB")


def test_build_snapshot_v2_full():
    snap = se.build_snapshot_v2(FakeClientV2(), coupon_lookahead_days=180,
                                tax_refund_amounts=[26000],
                                as_of="2026-08-26T18:30:00Z")
    assert snap["schema_version"] == "portfolio_snapshot.v2"
    assert set(snap) == {"schema_version", "as_of", "currency", "cash_value",
                         "positions", "payments", "performance", "source"}
    pos = snap["positions"][0]
    assert set(pos) == {"instrument_type", "secid", "name", "issuer_name",
                        "quantity", "market_value", "unit_price", "coupon_rate"}
    assert pos["issuer_name"] is None and pos["coupon_rate"] is None
    assert pos["market_value"] == 10220 and pos["unit_price"] == 1022  # 10*(101%*1000+12)
    perf = snap["performance"]
    assert perf["as_of"] == snap["as_of"]                 # обязано совпадать
    assert perf["annualized_return"] == 0.2757            # 27.57% → доли
    assert perf["net_contributions"] == 277000            # взносы − выводы
    assert perf["calculated_from"] == "2025-08-04T00:00:00Z"
    assert "ACC-SECRET" not in se._json_text(snap)        # без номера счёта


def test_build_snapshot_v2_null_performance():
    snap = se.build_snapshot_v2(FakeClientV2(xirr=None), as_of="2026-08-26T18:30:00Z")
    assert snap["performance"] is None                    # XIRR нет → null, не 0


# ---------------------- export_and_push v2 ----------------------

def test_export_and_push_v2_writes_and_posts(monkeypatch, tmp_path):
    fp = _patch(monkeypatch, [FakeResp(202, {"status": "accepted",
                                             "received_at": "2026-08-26T18:30:03Z"})])
    res, snap = se.export_and_push_portfolio(
        FakeClientV2(), url="http://x/ingest", secret="SEC",
        export_dir=str(tmp_path), coupon_lookahead_days=180,
        tax_refund_amounts=[26000], sleep=_nosleep)
    assert res.status == "accepted" and res.result == "accepted"
    assert snap["schema_version"] == "portfolio_snapshot.v2"
    assert (tmp_path / se.PORTFOLIO_V2_JSON).exists()          # пишем v2-файл
    assert not (tmp_path / se.PORTFOLIO_JSON).exists()         # v1 НЕ трогаем
    assert fp.calls[0]["data"] == se._json_text(snap).encode("utf-8")
    assert (tmp_path / se.ASOF_STATE).read_text().strip() == snap["as_of"]


def test_module_still_no_order_calls():
    import ast
    tree = ast.parse((APP_DIR / "bot" / "snapshot_export.py").read_text("utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in (
                "post_limit_buy", "post_market_buy", "post_order"):
            bad.append(node.attr)
        if (isinstance(node, ast.keyword) and node.arg == "trade"
                and isinstance(node.value, ast.Constant) and node.value.value is True):
            bad.append("trade=True")
    assert not bad
