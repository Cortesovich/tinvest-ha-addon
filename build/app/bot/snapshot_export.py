"""Read-only экспортёр обезличенных снимков для модуля скоринга и Mini App.

Три артефакта в папке экспорта (по умолчанию DATA_DIR/export):
  1) portfolio_snapshot.json    — форма входа --portfolio-snapshot прототипа;
  2) iis_allowlist_current.csv  — текущая ИИС-доступность инструментов;
  3) tbank_fundamentals_raw.csv — сырые поля GetAssetFundamentals (диагностика).

Плюс export_status.json — единственный источник правды о свежести (ok / stale /
not_connected по каждому артефакту).

ГРАНИЦЫ. Модуль ТОЛЬКО ЧИТАЕТ. Он не импортирует и не вызывает торговых методов
(PostOrder / post_market_buy / post_limit_buy), плана покупки и автопокупки.
Пригоден для токена без торговых прав. Транспорт наружу здесь НЕ реализован.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .config import DATA_DIR
from .tinvest_client import TInvestClient, TInvestError, _money, _ccy, _iso

log = logging.getLogger("export")

PORTFOLIO_JSON = "portfolio_snapshot.json"
ALLOWLIST_CSV = "iis_allowlist_current.csv"
FUNDAMENTALS_CSV = "tbank_fundamentals_raw.csv"
STATUS_JSON = "export_status.json"

_ALLOWLIST_HEADER = ["observed_at", "secid", "isin", "figi", "instrument_type",
                     "currency", "api_trade_available", "buy_available",
                     "sell_available", "source_method", "notes"]
_FUND_HEADER = ["observed_at", "secid", "isin", "api_field", "api_value",
                "unit", "period", "source_method", "notes"]

# Явная маркировка: снимок текущий, не доказывает доступность на ретро-дату.
_ALLOWLIST_NOTE = "current_snapshot; not_proof_of_availability_on_2026-06-30"

# Родовые названия типа счёта — не персональные данные; из скруббера исключаем,
# чтобы служебное слово в описании не считалось «утечкой имени счёта».
_GENERIC_ACCOUNT_NAMES = {"ИИС", "Брокерский счёт", "Брокерский", "Инвесткопилка"}


class ExportError(RuntimeError):
    pass


@dataclass
class ArtifactResult:
    name: str
    status: str            # ok | error
    path: str
    rows: int = 0
    error: str = ""


@dataclass
class ExportResult:
    dir: str
    status_path: str
    artifacts: list = field(default_factory=list)   # list[ArtifactResult]


# ----------------------------- утилиты формата -----------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(d) -> float | int:
    """Decimal → число JSON: целое как int, иначе float с 2 знаками."""
    q = Decimal(d).quantize(Decimal("0.01"))
    return int(q) if q == q.to_integral_value() else float(q)


def _flag(v):
    """API-флаг → 'true'/'false'/'' (пусто, если API поле не отдал)."""
    if v is None:
        return ""
    return "true" if bool(v) else "false"


def _valstr(v) -> str:
    """Значение фундаментала «как есть» в строку (без расчётов)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _json_text(obj) -> str:
    # детерминированно: сортировка ключей, UTF-8 без BOM, LF, отступ 2, \n в конце
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _csv_text(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def _atomic_write(path: Path, data: str) -> None:
    """Атомарная запись: temp в той же папке + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _assert_no_secrets(text: str, forbidden: list[str]) -> None:
    for token in forbidden:
        if token and token in text:
            raise ExportError("В выводе обнаружен запрещённый идентификатор")


# ----------------------------- сборка артефактов ---------------------------

def build_portfolio_snapshot(client: TInvestClient,
                             coupon_lookahead_days: int = 180) -> dict:
    """Снимок портфеля в форме входа --portfolio-snapshot (по фикстуре).

    market_value считаем ТОЛЬКО из текущей цены (никогда из средней цены
    покупки): облигации qty·(цена%/100·номинал+НКД), акции/ETF qty·currentPrice.
    Плавающие купоны (payOneBond=0) исключены — их сумма ещё не подтверждена.
    """
    raw = client.portfolio_raw()
    positions_raw = raw.get("positions", [])
    cash = _money(raw.get("totalAmountCurrencies"))
    currency = _ccy(raw.get("totalAmountPortfolio")) or "RUB"
    now = _utc_now_iso()

    bond_figis = [p.get("figi") for p in positions_raw
                  if p.get("instrumentType") == "bond" and p.get("figi")]
    prices = client._last_prices(bond_figis) if bond_figis else {}

    positions: list[dict] = []
    for p in positions_raw:
        itype = p.get("instrumentType")
        figi = p.get("figi")
        qty = _money(p.get("quantity"))
        if not figi or qty <= 0 or itype not in ("bond", "share", "etf"):
            continue
        try:
            if itype == "bond":
                bond = client._bond_info(figi)
                nominal = _money(bond.get("nominal"))
                aci = _money(bond.get("aciValue"))
                price_pct = prices.get(figi) or Decimal(100)
                value = qty * (price_pct / Decimal(100) * nominal + aci)
                name = bond.get("name") or ""
                secid = bond.get("ticker") or ""
            else:
                inst = client.instrument_brief(figi)
                value = qty * _money(p.get("currentPrice"))
                name = inst.get("name") or ""
                secid = inst.get("ticker") or ""
        except TInvestError as e:
            log.warning("Инструмент %s пропущен в снимке: %s", figi, e)
            continue
        positions.append({
            "instrument_type": itype,
            "secid": secid,
            "name": name,
            "quantity": _num(qty),
            "market_value": _num(value),
        })

    payments: list[dict] = []
    for c in client.get_upcoming_coupons(coupon_lookahead_days, hide_zero=True):
        payments.append({
            "amount": _num(c.total),
            "currency": c.currency or "RUB",
            "kind": "coupon",
            "name": c.bond_name,
            "payment_at": _iso(c.date),
            "secid": c.ticker or "",
        })

    positions.sort(key=lambda x: (x["instrument_type"], x["secid"]))
    payments.sort(key=lambda x: (x["payment_at"], x["secid"]))
    return {
        "as_of": now,
        "cash_value": _num(cash),
        "currency": currency,
        "payments": payments,
        "positions": positions,
        "source": {
            "description": ("Обезличенный live read-only снимок структуры "
                            "инвестиционного счёта; без номера счёта, токенов "
                            "и торговых команд."),
            "kind": "live_read_only",
            "loaded_at": now,
        },
    }


def build_allowlist_rows(client: TInvestClient) -> list[list]:
    now = _utc_now_iso()
    out: list[list] = []
    for r in client.iis_universe_rows():
        out.append([
            now, r["secid"], r["isin"], r["figi"], r["instrument_type"],
            r["currency"], _flag(r["api_trade_available"]),
            _flag(r["buy_available"]), _flag(r["sell_available"]),
            "InstrumentsService.Bonds/Shares", _ALLOWLIST_NOTE,
        ])
    out.sort(key=lambda x: (x[4], x[1]))   # instrument_type, secid
    return out


def build_fundamentals_rows(client: TInvestClient,
                            whitelist_tickers: list[str]) -> list[list]:
    now = _utc_now_iso()
    out: list[list] = []
    for r in client.raw_fundamentals(whitelist_tickers):
        out.append([
            now, r["secid"], r["isin"], r["api_field"], _valstr(r["api_value"]),
            "", "",   # unit/period API не сообщает — не выдумываем
            "InstrumentsService.GetAssetFundamentals", "",
        ])
    out.sort(key=lambda x: (x[1], x[3]))   # secid, api_field
    return out


# ----------------------------- оркестрация ---------------------------------

def _read_status(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _apply_ok(status: dict, key: str, now: str) -> None:
    prev = status.get(key, {})
    status[key] = {"status": "ok", "last_success_utc": now,
                   "last_error_utc": prev.get("last_error_utc", ""), "error": ""}


def _apply_err(status: dict, key: str, now: str, err: str) -> None:
    prev = status.get(key, {})
    had = bool(prev.get("last_success_utc"))
    status[key] = {"status": "stale" if had else "not_connected",
                   "last_success_utc": prev.get("last_success_utc", ""),
                   "last_error_utc": now, "error": err}


def run_export(client: TInvestClient, *, export_dir: str | None = None,
               coupon_lookahead_days: int = 180,
               fundamentals_scope: str = "whitelist",
               stock_whitelist: list[str] | None = None) -> ExportResult:
    """Собрать три снимка. Ошибка одного артефакта НЕ перезаписывает его прошлый
    хороший файл и не роняет остальные; статус фиксируется в export_status.json.
    """
    out_dir = Path(export_dir) if export_dir else (DATA_DIR / "export")
    out_dir.mkdir(parents=True, exist_ok=True)
    now = _utc_now_iso()
    status = _read_status(out_dir / STATUS_JSON)
    results: list[ArtifactResult] = []

    # запрещённые к выводу идентификаторы (резолвим счёт, но НЕ публикуем).
    # acc_id скрубим всегда; имя — только если оно НЕ родовой ярлык типа счёта.
    forbidden: list[str] = []
    try:
        acc_id, acc_name = client.resolve_account()
        forbidden = [acc_id]
        if acc_name and acc_name not in _GENERIC_ACCOUNT_NAMES:
            forbidden.append(acc_name)
    except TInvestError:
        pass

    # 1) portfolio_snapshot.json
    ar = ArtifactResult("portfolio", "ok", str(out_dir / PORTFOLIO_JSON))
    try:
        snap = build_portfolio_snapshot(client, coupon_lookahead_days)
        text = _json_text(snap)
        _assert_no_secrets(text, forbidden)
        _atomic_write(out_dir / PORTFOLIO_JSON, text)
        ar.rows = len(snap["positions"])
        _apply_ok(status, "portfolio", now)
    except Exception as e:  # noqa: BLE001
        ar.status, ar.error = "error", str(e)
        _apply_err(status, "portfolio", now, str(e))
        log.warning("Экспорт портфеля не удался: %s", e)
    results.append(ar)

    # 2) iis_allowlist_current.csv
    ar = ArtifactResult("allowlist", "ok", str(out_dir / ALLOWLIST_CSV))
    try:
        rows = build_allowlist_rows(client)
        _atomic_write(out_dir / ALLOWLIST_CSV, _csv_text(_ALLOWLIST_HEADER, rows))
        ar.rows = len(rows)
        _apply_ok(status, "allowlist", now)
    except Exception as e:  # noqa: BLE001
        ar.status, ar.error = "error", str(e)
        _apply_err(status, "allowlist", now, str(e))
        log.warning("Экспорт allowlist не удался: %s", e)
    results.append(ar)

    # 3) tbank_fundamentals_raw.csv
    ar = ArtifactResult("fundamentals", "ok", str(out_dir / FUNDAMENTALS_CSV))
    try:
        if fundamentals_scope == "none":
            rows = []
        else:
            rows = build_fundamentals_rows(client, stock_whitelist or [])
        _atomic_write(out_dir / FUNDAMENTALS_CSV, _csv_text(_FUND_HEADER, rows))
        ar.rows = len(rows)
        _apply_ok(status, "fundamentals", now)
    except Exception as e:  # noqa: BLE001
        ar.status, ar.error = "error", str(e)
        _apply_err(status, "fundamentals", now, str(e))
        log.warning("Экспорт fundamentals не удался: %s", e)
    results.append(ar)

    status["generated_at"] = now
    _atomic_write(out_dir / STATUS_JSON, _json_text(status))
    return ExportResult(str(out_dir), str(out_dir / STATUS_JSON), results)
