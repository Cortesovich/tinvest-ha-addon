"""Форматирование ответов для Telegram (parse_mode=HTML).

ВАЖНО: любые данные от API (названия бумаг и т.п.) прогоняем через esc(),
иначе символ '<' молча ломает отправку сообщения (parse_mode=HTML).
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .tinvest_client import Balance, CouponItem, MaturityItem

# Запасные фикс-смещения на случай, если в системе нет базы поясов (Windows
# без пакета tzdata). Москва с 2014 г. — постоянный UTC+3 без переходов.
_TZ_FALLBACK = {
    "Europe/Moscow": timezone(timedelta(hours=3)),
    "Europe/Kaliningrad": timezone(timedelta(hours=2)),
    "Europe/Samara": timezone(timedelta(hours=4)),
    "Asia/Yekaterinburg": timezone(timedelta(hours=5)),
}


def _zone(tz: str):
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError, ValueError, OSError):
        return _TZ_FALLBACK.get(tz, timezone.utc)


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _num(x: Decimal, dp: int = 2) -> str:
    q = Decimal(10) ** -dp
    s = f"{x.quantize(q):,.{dp}f}".replace(",", " ")
    return s


def _dt(d: datetime | None, tz: str) -> str:
    if d is None:
        return "—"
    return d.astimezone(_zone(tz)).strftime("%d.%m.%Y")


def format_balance(b: Balance) -> str:
    return (
        f"💼 <b>{esc(b.account_name)}</b>\n"
        f"Стоимость: <b>{_num(b.total)} {esc(b.currency)}</b>\n"
        f"\n"
        f"Облигации: {_num(b.bonds)}\n"
        f"Акции: {_num(b.shares)}\n"
        f"Фонды: {_num(b.etf)}\n"
        f"Свободные деньги: {_num(b.money)}\n"
        f"\n"
        f"<i>Годовая доходность — /income</i>"
    )


_MONTHS_RU = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


def format_coupons(items: list[CouponItem], tz: str, days: int) -> str:
    if not items:
        return f"Купонных выплат в ближайшие {days} дн. не найдено."
    lines = [f"🎟 <b>Ближайшие купоны ({days} дн.)</b>"]
    total_by_ccy: dict[str, Decimal] = {}
    cur_key = None                      # (год, месяц) текущей группы
    month_by_ccy: dict[str, Decimal] = {}

    def _month_subtotal():
        if month_by_ccy:
            s = ", ".join(f"{_num(v)} {esc(k)}" for k, v in month_by_ccy.items())
            lines.append(f"    <i>за месяц: {s}</i>")

    for c in items:
        d = c.date.astimezone(_zone(tz))
        key = (d.year, d.month)
        if key != cur_key:
            _month_subtotal()           # подытог за предыдущий месяц
            cur_key = key
            month_by_ccy = {}
            lines.append(f"\n<b>── {_MONTHS_RU[d.month]} {d.year} ──</b>")
        lines.append(
            f"{_dt(c.date, tz)} — {esc(c.bond_name)}\n"
            f"    {_num(c.total)} {esc(c.currency)} "
            f"({_num(c.per_bond)} × {_num(c.quantity, 0)})"
        )
        total_by_ccy[c.currency] = total_by_ccy.get(c.currency, Decimal(0)) + c.total
        month_by_ccy[c.currency] = month_by_ccy.get(c.currency, Decimal(0)) + c.total
    _month_subtotal()                   # подытог за последний месяц
    summary = ", ".join(f"{_num(v)} {esc(k)}" for k, v in total_by_ccy.items())
    lines.append(f"\nИтого к выплате: <b>{summary}</b>")
    return "\n".join(lines)


def format_maturities(items: list[MaturityItem], tz: str) -> str:
    if not items:
        return "Облигаций на счёте не найдено."
    lines = ["📅 <b>Сроки погашения</b>\n"]
    for m in items:
        left = f"{m.days_left} дн." if m.days_left >= 0 else "—"
        lines.append(
            f"{_dt(m.maturity, tz)} (через {left}) — {esc(m.bond_name)}\n"
            f"    {_num(m.quantity, 0)} шт. × номинал {_num(m.nominal)} {esc(m.currency)}"
        )
    return "\n".join(lines)


# Красивые имена эмитентов для вывода (ключ концентрации — в нижнем регистре).
_ISSUER_DISPLAY = {
    "офз": "ОФЗ", "ржд": "РЖД", "втб": "ВТБ", "рсхб": "РСХБ", "вэб": "ВЭБ",
    "мтс": "МТС", "гмк": "ГМК", "фск": "ФСК", "x5": "X5", "икс 5": "Икс 5",
    "дом.рф": "ДОМ.РФ", "дом рф": "ДОМ РФ",
}


def _issuer_display(key: str) -> str:
    k = (key or "").lower()
    if k in _ISSUER_DISPLAY:
        return _ISSUER_DISPLAY[k]
    return key[:1].upper() + key[1:] if key else key


def format_holdings(by_issuer: dict, total: Decimal, cap_pct: float) -> str:
    """Купленные облигации по компаниям: доля от облигационной части, по убыванию."""
    if not by_issuer or total <= 0:
        return "Облигаций на счёте не найдено."
    rows = sorted(by_issuer.items(), key=lambda kv: kv[1], reverse=True)
    cap = Decimal(str(cap_pct)) if cap_pct and cap_pct > 0 else None
    lines = ["🏦 <b>Облигации по компаниям</b>",
             f"Всего облигаций: {_num(total)} ₽\n"]
    for iss, val in rows:
        share = (val / total * Decimal(100)) if total > 0 else Decimal(0)
        over = cap is not None and iss != "ОФЗ" and share >= cap
        name = _issuer_display(iss)
        name = f"<b>{esc(name)}</b>" if over else esc(name)
        lines.append(f"{name} — {share:.1f}% · {_num(val)} ₽{' ⚠️' if over else ''}")
    note = "<i>Доля от облигационной части."
    if cap is not None:
        note += f" ⚠️ = достигнут лимит {_num(cap, 0)}% на компанию (ОФЗ без лимита)."
    note += "</i>"
    lines.append("\n" + note)
    return "\n".join(lines)


def format_return(r, tz: str) -> str:
    xirr = f"{r.xirr_pct:+.2f} % годовых" if r.xirr_pct is not None else "—"
    since = _dt(r.since, tz)
    lines = [
        f"📈 <b>Доходность — {esc(r.account_name)}</b>",
        f"XIRR с {since}: <b>{esc(xirr)}</b>",
        "",
        f"Стоимость сейчас: {_num(r.nav)} {esc(r.currency)}",
        f"Свои взносы: {_num(r.contributed)}",
    ]
    if r.withdrawn > 0:
        lines.append(f"Выведено: {_num(r.withdrawn)}")
    lines += [
        f"Прибыль: <b>{_num(r.profit)} {esc(r.currency)}</b>",
        "",
        f"— купоны: {_num(r.coupons)}",
    ]
    if r.dividends > 0:
        lines.append(f"— дивиденды: {_num(r.dividends)}")
    if r.tax_refund > 0:
        lines.append(f"— налоговый вычет: {_num(r.tax_refund)}")
    if r.taxes != 0:
        lines.append(f"— налоги: {_num(r.taxes)}")
    return "\n".join(lines)


def format_reinvest(plan_items, free_cash: Decimal, remaining: Decimal,
                    min_cash: float, tz: str, market_open=None,
                    can_buy: bool = False) -> str:
    if free_cash < Decimal(str(min_cash)):
        return (f"Свободных рублей: {_num(free_cash)}. Для реинвеста нужно хотя бы "
                f"{_num(Decimal(str(min_cash)))} ₽ — пока копим купоны.")
    if not plan_items:
        return ("Подходящих облигаций не найдено по текущим фильтрам "
                "(надёжность / срок / цена).")

    planned = free_cash - remaining
    lines = ["🧮 <b>Кандидаты на реинвест</b>",
             f"Свободно: {_num(free_cash)} ₽\n"]
    if market_open is False:
        lines.insert(1, "⚠️ Биржа закрыта — цены на момент последних торгов.")
    for i, it in enumerate(plan_items, 1):
        c = it.candidate
        cpn = f"купон ~{c.coupon_annual_pct:.1f}%" if c.coupon_annual_pct else "купон —"
        tag = "ОФЗ" if c.is_ofz else "корп."
        unit = "лот" if c.lot != 1 else "шт"
        buy = (f"взять {it.lots} {unit} ≈ {_num(it.cost)} ₽" if it.lots > 0
               else "не хватает на 1 шт")
        lines.append(
            f"{i}. <b>{esc(c.name)}</b> · {tag}\n"
            f"    <b>YTM {c.ytm_pct:.2f}%</b> · цена {_num(c.price_pct)}% · {cpn}\n"
            f"    до {_dt(c.maturity, tz)} ({c.days_left} дн.) · {buy}"
        )
    lines.append(f"\nИтого к покупке: <b>{_num(planned)} ₽</b> · "
                 f"останется {_num(remaining)} ₽")
    if not can_buy:
        lines.append("\n<i>Только предложение. Кнопка покупки появляется в "
                     "торговые часы при включённом trade_enabled.</i>")
    return "\n".join(lines)


def format_topbonds(candidates, n: int, tz: str) -> str:
    top = candidates[:n]
    if not top:
        return "Подходящих облигаций не найдено по фильтрам."
    lines = [f"🏆 <b>Топ-{len(top)} облигаций по YTM</b>\n"]
    for i, c in enumerate(top, 1):
        tag = "ОФЗ" if c.is_ofz else "корп."
        lines.append(
            f"{i}. <b>{esc(c.name)}</b> · {tag}\n"
            f"    <b>YTM {c.ytm_pct:.2f}%</b> · цена {_num(c.price_pct)}% · "
            f"до {_dt(c.maturity, tz)} ({c.days_left} дн.)"
        )
    lines.append("\n<i>Только список, без покупки.</i>")
    return "\n".join(lines)


def _pct(x, dp=0):
    return "—" if x is None else f"{x:.{dp}f}%"


def format_topstocks(scores, n: int, bond_ytm) -> str:
    top = scores[:n]
    if not top:
        return ("Не удалось собрать данные по акциям (нет фундаментала или все "
                "отсеяны фильтрами). Проверь список тикеров.")
    hdr = f"🏆 <b>Топ-{len(top)} акций по скору</b>"
    if bond_ytm is not None:
        hdr += f"\nСтавка сравнения: облигация ~{bond_ytm:.1f}% YTM"
    lines = [hdr, ""]
    for i, s in enumerate(top, 1):
        if s.premium_vs_bond is None:
            prem = "— (нет надёжного P/E)"
        else:
            flag = "🟢 E/P выше YTM" if s.premium_vs_bond > 0 else "🔴 E/P ниже YTM"
            prem = f"{s.premium_vs_bond:+.1f} п.п. · {flag}"
        pe = f"{s.pe:.1f}" if (s.pe and s.pe >= 1) else "н/д"
        debt = "н/д" if (s.debt in (None, 0)) else _pct(s.debt)
        lines.append(
            f"{i}. <b>{esc(s.name)}</b> ({esc(s.ticker)}) · скор <b>{s.total:.0f}</b>\n"
            f"    P/E {pe} · рост {_pct(s.growth)} · долг/кап {debt} · "
            f"ROE {_pct(s.roe)} · дивы {_pct(s.div, 1)}\n"
            f"    E/P − YTM: {prem}"
        )
    lines.append("\n<i>Полная таблица — stocks_score.csv в папке бота. "
                 "Своё мнение по компаниям правь в stock_opinions.csv.</i>")
    return "\n".join(lines)


def format_order_report(results) -> str:
    """results: список (name, lots, resp|error). Формирует отчёт по заявкам."""
    lines = ["📥 <b>Результат покупки</b>\n"]
    for name, lots, res in results:
        if isinstance(res, Exception):
            lines.append(f"❌ {esc(name)} ×{lots}: {esc(res)}")
            continue
        st = (res.get("executionReportStatus") or "").replace(
            "EXECUTION_REPORT_STATUS_", "")
        done = res.get("lotsExecuted", 0)
        amt = res.get("totalOrderAmount") or res.get("executedOrderPrice")
        rub = _num(_moneyval(amt)) if amt else "—"
        human = {"FILL": "исполнена", "PARTIALLYFILL": "частично",
                 "NEW": "принята", "REJECTED": "отклонена",
                 "CANCELLED": "отменена"}.get(st, st or "?")
        icon = "✅" if st in ("FILL", "PARTIALLYFILL", "NEW") else "⚠️"
        lines.append(f"{icon} {esc(name)}: {human}, лотов {done}, ~{rub} ₽")
    return "\n".join(lines)


def _moneyval(m) -> Decimal:
    if not isinstance(m, dict):
        return Decimal(0)
    return Decimal(int(m.get("units", 0) or 0)) + Decimal(int(m.get("nano", 0) or 0)) / Decimal(1_000_000_000)


def format_deposits(items, tz: str) -> str:
    if not items:
        return "Пополнений не найдено."
    lines = ["💰 <b>Пополнения счёта</b>\n"]
    total = Decimal(0)
    for d in items:
        mark = "  ← вычет" if d.is_refund else ""
        lines.append(f"{_dt(d.date, tz)} — {_num(d.amount)} {esc(d.currency)}{mark}")
        total += d.amount
    lines.append(f"\nВсего пополнено: <b>{_num(total)}</b>")
    lines.append("\nЧтобы засчитать вычет как доход — впиши его сумму в "
                 "config.yaml → tax_refund_amounts.")
    return "\n".join(lines)


def format_export_summary(res) -> str:
    """Обезличенная сводка экспорта: счётчики, статусы, пути. Без позиций/сумм."""
    icon = {"ok": "✅", "error": "⚠️"}
    lines = ["📤 <b>Экспорт read-only снимков</b>\n"]
    for a in res.artifacts:
        tail = f", строк {a.rows}" if a.status == "ok" else f" — {esc(a.error)}"
        lines.append(f"{icon.get(a.status, '•')} {esc(a.name)}: {a.status}{tail}")
    lines.append(f"\nПапка: <code>{esc(res.dir)}</code>")
    lines.append("Свежесть — в <code>export_status.json</code>.")
    return "\n".join(lines)


HELP_TEXT = (
    "🤖 <b>ИИС-бот (только чтение)</b>\n\n"
    "/balance — баланс и структура счёта\n"
    "/income — годовая доходность (XIRR) и прибыль\n"
    "/coupons — ближайшие купонные выплаты\n"
    "/maturity — сроки погашения облигаций\n"
    "/holdings — облигации по компаниям (доля, лимит 20%)\n"
    "/topbonds — топ-5 облигаций по YTM (без покупки)\n"
    "/topstocks — топ-10 акций по скору (без покупки)\n"
    "/reinvest — подбор покупки облигаций с учётом баланса\n"
    "/deposits — история пополнений\n"
    "/accounts — список доступных счетов\n"
    "/export — read-only снимки для Mini App (владелец)\n"
    "/help — эта справка"
)
