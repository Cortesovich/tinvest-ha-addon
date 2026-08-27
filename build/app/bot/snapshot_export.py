"""Read-only экспортёр обезличенных снимков для модуля скоринга и Mini App.

Три артефакта в папке экспорта (по умолчанию DATA_DIR/export):
  1) portfolio_snapshot.json    — форма входа --portfolio-snapshot прототипа;
  2) iis_allowlist_current.csv  — текущая ИИС-доступность инструментов;
  3) tbank_fundamentals_raw.csv — сырые поля GetAssetFundamentals (диагностика).

Плюс export_status.json — единственный источник правды о свежести (ok / stale /
not_connected по каждому артефакту).

ГРАНИЦЫ. Модуль ТОЛЬКО ЧИТАЕТ. Он не импортирует и не вызывает торговых методов
(PostOrder / post_market_buy / post_limit_buy), плана покупки и автопокупки.
Пригоден для токена без торговых прав. Здесь же — исходящий push снимка
портфеля в приёмник Mini App (push_snapshot): с нашей стороны это тоже только
чтение T-Invest + отправка обезличенного снимка (никаких торговых вызовов).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests

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
    forbidden = _forbidden_identifiers(client)

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


# ===================== исходящий push снимка в Mini App =====================
# Только чтение T-Invest + обезличенный POST. Никаких торговых вызовов.

SCHEMA_V2 = "portfolio_snapshot.v2"
PORTFOLIO_V2_JSON = "portfolio_snapshot.v2.json"
ASOF_STATE = "push_asof.txt"          # монотонный as_of последнего push


@dataclass
class PushResult:
    status: str        # accepted | unchanged | error | network (наша категория)
    code: int          # HTTP-код (0 если сеть)
    message: str = ""
    result: str = ""   # 'status'/'error' из тела приёмника (accepted/stale_snapshot/…)


def _forbidden_identifiers(client: TInvestClient) -> list[str]:
    """acc_id (всегда) + имя счёта, если оно НЕ родовой ярлык типа счёта."""
    try:
        acc_id, acc_name = client.resolve_account()
    except TInvestError:
        return []
    out = [acc_id]
    if acc_name and acc_name not in _GENERIC_ACCOUNT_NAMES:
        out.append(acc_name)
    return out


# Коды, которые НЕ повторяем автоматически (контракт/доступ/данные).
_PUSH_NO_RETRY = {401, 409, 413, 415, 422}


def _parse_ingest_body(r) -> tuple[str, str | None]:
    """Из ответа приёмника — result ('status'/'error') и received_at.
    Суммы портфеля из тела в лог НЕ тянем."""
    try:
        b = r.json()
    except Exception:  # noqa: BLE001
        return "", None
    if not isinstance(b, dict):
        return "", None
    return (str(b.get("status") or b.get("error") or ""), b.get("received_at"))


def _log_ingest(http, result: str, as_of: str, received_at) -> None:
    # безопасная строка: без секрета и без сумм портфеля
    log.info("mini_app_ingest status=%s result=%s as_of=%s schema=%s received_at=%s",
             http, result or "-", as_of or "null", SCHEMA_V2, received_at or "null")


def push_snapshot(url: str, secret: str, json_text: str, *, as_of: str = "",
                  attempts: int = 3, timeout: int = 15,
                  sleep=time.sleep) -> PushResult:
    """POST снимка на ingest-приёмник Mini App (контракт Codex).

    202 accepted / 200 unchanged — успех. 401/409/413/415/422 — НЕ повторяем
    (контракт/доступ/данные). Сеть/429/5xx — повтор с задержкой. Секрет НЕ
    логируем; в лог — безопасная строка mini_app_ingest без секрета и сумм.
    """
    headers = {"Authorization": f"Bearer {secret}",
               "Content-Type": "application/json"}
    body = json_text.encode("utf-8")
    backoff = [3, 8]                      # задержки перед 2-й и 3-й попыткой, сек
    out = last = PushResult("error", 0, "не отправлено")
    for i in range(attempts):
        received_at = None
        try:
            r = requests.post(url, data=body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            out = last = PushResult("network", 0, str(e)[:200], "network_error")
            retryable = True
        else:
            code = r.status_code
            result, received_at = _parse_ingest_body(r)
            if code == 202:
                out, retryable = PushResult("accepted", 202, "новый снимок",
                                            result or "accepted"), False
            elif code == 200:
                out, retryable = PushResult("unchanged", 200, "не изменился",
                                            result or "unchanged"), False
            elif code in _PUSH_NO_RETRY:
                out, retryable = PushResult("error", code, "", result), False
            elif code == 429 or 500 <= code < 600:
                out = last = PushResult("error", code, "", result)
                retryable = True
            else:
                out, retryable = PushResult("error", code, "", result), False
        if retryable and i < attempts - 1:
            sleep(backoff[min(i, len(backoff) - 1)])
            continue
        _log_ingest(out.code, out.result or out.status, as_of, received_at)
        return out
    _log_ingest(last.code, last.result or last.status, as_of, None)
    return last


def _next_push_as_of(out_dir: Path) -> str:
    """Монотонный as_of: время сейчас, но строго больше предыдущего (при
    совпадении/откате — предыдущий + 1с). Пишем ДО отправки."""
    p = out_dir / ASOF_STATE
    now = _utc_now_iso()
    try:
        last = p.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        last = ""
    if last and now <= last:
        try:
            dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
            now = (dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    try:
        _atomic_write(p, now + "\n")
    except Exception:  # noqa: BLE001
        log.warning("Не удалось записать push_asof.txt")
    return now


def _build_performance(client: TInvestClient, tax_refund_amounts, as_of: str):
    """performance из готового расчёта /income (compute_return), БЕЗ пересчёта
    формулы. Если XIRR не вычислен — None. performance.as_of == корневому as_of."""
    try:
        r = client.compute_return(tax_refund_amounts or [])
    except TInvestError as e:
        log.warning("performance недоступен: %s", e)
        return None
    if r.xirr_pct is None:
        return None
    return {
        "method": "xirr",
        "calculated_from": _iso(r.since) if r.since else None,
        "as_of": as_of,
        "annualized_return": round(float(r.xirr_pct) / 100.0, 6),
        "net_contributions": _num(r.contributed - r.withdrawn),
        "profit": _num(r.profit),
        "coupons": _num(r.coupons),
        "tax_deductions": _num(r.tax_refund),
        "currency": r.currency,
    }


def build_snapshot_v2(client: TInvestClient, *, coupon_lookahead_days: int = 180,
                      tax_refund_amounts=None, as_of: str) -> dict:
    """Снимок v2 для Mini App: v1 + schema_version + performance + поля позиций.

    Локальный v1 (вход скоринг-модуля) НЕ меняем — v2 идёт только в push.
    unit_price — цена одной бумаги в RUB (market_value/кол-во). issuer_name и
    coupon_rate — null: у API нет надёжного прямого поля (контракт допускает null).
    """
    v1 = build_portfolio_snapshot(client, coupon_lookahead_days)
    positions = []
    for p in v1["positions"]:
        qty = p["quantity"]
        mv = p["market_value"]
        unit_price = round(mv / qty, 4) if qty else None
        positions.append({
            "instrument_type": p["instrument_type"],
            "secid": p["secid"],
            "name": p["name"],
            "issuer_name": None,
            "quantity": qty,
            "market_value": mv,
            "unit_price": unit_price,
            "coupon_rate": None,
        })
    return {
        "schema_version": SCHEMA_V2,
        "as_of": as_of,
        "currency": v1["currency"],
        "cash_value": v1["cash_value"],
        "positions": positions,
        "payments": v1["payments"],
        "performance": _build_performance(client, tax_refund_amounts, as_of),
        "source": {
            "kind": "live_read_only",
            "description": "Обезличенный read-only снимок портфеля",
            "loaded_at": _utc_now_iso(),
        },
    }


def export_and_push_portfolio(client: TInvestClient, *, url: str, secret: str,
                              export_dir: str | None = None,
                              coupon_lookahead_days: int = 180,
                              tax_refund_amounts=None,
                              sleep=time.sleep) -> tuple[PushResult, dict]:
    """Собрать СВЕЖИЙ снимок v2, записать локально (portfolio_snapshot.v2.json,
    НЕ трогая v1) и запушить с монотонным as_of. Скруббер — перед отправкой.
    """
    out_dir = Path(export_dir) if export_dir else (DATA_DIR / "export")
    out_dir.mkdir(parents=True, exist_ok=True)
    as_of = _next_push_as_of(out_dir)
    snap = build_snapshot_v2(client, coupon_lookahead_days=coupon_lookahead_days,
                             tax_refund_amounts=tax_refund_amounts, as_of=as_of)
    text = _json_text(snap)
    _assert_no_secrets(text, _forbidden_identifiers(client))
    _atomic_write(out_dir / PORTFOLIO_V2_JSON, text)
    res = push_snapshot(url, secret, text, as_of=as_of, sleep=sleep)
    return res, snap
