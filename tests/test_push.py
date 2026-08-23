"""Оффлайн-тесты исходящего push снимка портфеля (без сети).

Проверяют матрицу статусов/повторов по контракту приёмника Codex:
202 accepted, 200 unchanged, 401/409/413/415/422 — без повтора,
сеть/429/5xx — с повтором; заголовки/тело; сборка+запись+отправка.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

APP_DIR = Path(__file__).resolve().parents[1] / "build" / "app"
sys.path.insert(0, str(APP_DIR))

from bot import snapshot_export as se   # noqa: E402


def _noscleep(_s):   # без задержек в тестах
    return None


class FakeResp:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text


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


def test_accepted_202(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(202)])
    res = se.push_snapshot("http://x/ingest", "SEC", '{"a":1}', sleep=_noscleep)
    assert (res.status, res.code) == ("accepted", 202)
    assert len(fp.calls) == 1


def test_unchanged_200(monkeypatch):
    _patch(monkeypatch, [FakeResp(200)])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_noscleep)
    assert (res.status, res.code) == ("unchanged", 200)


@pytest.mark.parametrize("code", [401, 409, 413, 415, 422])
def test_no_retry_codes(monkeypatch, code):
    fp = _patch(monkeypatch, [FakeResp(code, "nope")])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_noscleep)
    assert res.status == "error" and res.code == code
    assert len(fp.calls) == 1          # НЕ повторяли


def test_retry_429_then_accepted(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(429), FakeResp(202)])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_noscleep)
    assert res.status == "accepted"
    assert len(fp.calls) == 2


def test_5xx_exhausts(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(503), FakeResp(500), FakeResp(502)])
    res = se.push_snapshot("http://x", "SEC", "{}", attempts=3, sleep=_noscleep)
    assert res.status == "error" and res.code == 502
    assert len(fp.calls) == 3


def test_network_then_accepted(monkeypatch):
    fp = _patch(monkeypatch, [requests.ConnectionError("boom"), FakeResp(202)])
    res = se.push_snapshot("http://x", "SEC", "{}", sleep=_noscleep)
    assert res.status == "accepted"
    assert len(fp.calls) == 2


def test_network_exhausts(monkeypatch):
    fp = _patch(monkeypatch, [requests.ConnectionError("a"),
                              requests.Timeout("b"),
                              requests.ConnectionError("c")])
    res = se.push_snapshot("http://x", "SEC", "{}", attempts=3, sleep=_noscleep)
    assert res.status == "network"
    assert len(fp.calls) == 3


def test_headers_and_raw_body(monkeypatch):
    fp = _patch(monkeypatch, [FakeResp(202)])
    se.push_snapshot("http://x/ingest", "TOPSECRET", '{"k":"v"}', sleep=_noscleep)
    h = fp.calls[0]["headers"]
    assert h["Authorization"] == "Bearer TOPSECRET"
    assert h["Content-Type"] == "application/json"
    assert fp.calls[0]["data"] == b'{"k":"v"}'   # голое тело, без обёртки


def test_export_and_push_builds_writes_posts(monkeypatch, tmp_path):
    snap = {"as_of": "2026-08-23T00:00:00Z", "cash_value": 100,
            "currency": "RUB", "payments": [], "positions": [],
            "source": {"kind": "live_read_only"}}
    monkeypatch.setattr(se, "build_portfolio_snapshot", lambda client, days: snap)
    monkeypatch.setattr(se, "_forbidden_identifiers", lambda client: [])
    fp = _patch(monkeypatch, [FakeResp(202)])
    res, out = se.export_and_push_portfolio(
        None, url="http://x/ingest", secret="SEC",
        export_dir=str(tmp_path), coupon_lookahead_days=180, sleep=_noscleep)
    assert res.status == "accepted" and out is snap
    assert (tmp_path / se.PORTFOLIO_JSON).exists()             # записан локально
    assert fp.calls[0]["data"] == se._json_text(snap).encode("utf-8")  # то же тело
