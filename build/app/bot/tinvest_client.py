"""Клиент T-Invest API через прямой REST (только requests).

Никаких сторонних SDK — только официальный HTTP-эндпоинт банка и токен.
Читаем: счета, портфель, купоны, сроки погашения облигаций.
Read-only токен полностью покрывает все используемые здесь методы.

REST-протокол: POST на
  https://invest-public-api.tbank.ru/rest/<Service>/<Method>
с заголовком Authorization: Bearer <token>, тело и ответ — JSON.
Денежные значения приходят как {"currency","units","nano"}, где units —
СТРОКА (int64), nano — целое; величина = units + nano/1e9.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests

log = logging.getLogger("tinvest")

# Опционально: доверять сертификатам из хранилища Windows (там есть российский
# Национальный УЦ, которым подписан tbank.ru). Если пакет не установлен —
# просто работаем через рабочий хост tinkoff.ru. Установить при желании:
#   pip install truststore
try:  # pragma: no cover
    import truststore
    truststore.inject_into_ssl()
    log.info("truststore активен — используется хранилище сертификатов Windows")
except Exception:
    pass

# Хосты API. tbank.ru — основной (с 2025 старый tinkoff.ru отключён Т-Банком).
# tbank.ru подписан УЦ Минцифры — доверяем через общий CA-бандл, собранный в
# образе (см. Dockerfile: REQUESTS_CA_BUNDLE). tinkoff.ru оставлен запасным.
API_HOSTS = [
    "https://invest-public-api.tbank.ru/rest",
    "https://invest-public-api.tinkoff.ru/rest",
]
_CONTRACT = "tinkoff.public.invest.api.contract.v1"

# Тип счёта (строковые enum'ы REST-ответа)
ACCOUNT_TYPE_IIS = "ACCOUNT_TYPE_TINKOFF_IIS"
ACCOUNT_TYPE_BROKER = "ACCOUNT_TYPE_TINKOFF"
_TYPE_HUMAN = {
    ACCOUNT_TYPE_BROKER: "Брокерский",
    ACCOUNT_TYPE_IIS: "ИИС",
    "ACCOUNT_TYPE_INVEST_BOX": "Инвесткопилка",
    "ACCOUNT_TYPE_INVEST_FUND": "Фонд",
}
_SELECTOR_TO_TYPE = {"iis": ACCOUNT_TYPE_IIS, "broker": ACCOUNT_TYPE_BROKER}


# ---------- преобразования ----------

def _money(v) -> Decimal:
    """MoneyValue/Quotation -> Decimal. units может прийти строкой."""
    if not v:
        return Decimal(0)
    units = int(v.get("units", 0) or 0)
    nano = int(v.get("nano", 0) or 0)
    return Decimal(units) + Decimal(nano) / Decimal(1_000_000_000)


def _ccy(v) -> str:
    return (v.get("currency", "") if isinstance(v, dict) else "").upper()


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- структуры вывода (совместимы с formatters.py) ----------

@dataclass
class Balance:
    account_name: str
    account_id: str
    total: Decimal
    currency: str
    bonds: Decimal
    shares: Decimal
    etf: Decimal
    money: Decimal
    expected_yield_pct: Decimal


@dataclass
class CouponItem:
    bond_name: str
    ticker: str
    date: datetime
    per_bond: Decimal
    quantity: Decimal
    total: Decimal
    currency: str


@dataclass
class MaturityItem:
    bond_name: str
    ticker: str
    maturity: datetime | None
    quantity: Decimal
    nominal: Decimal
    currency: str
    days_left: int


@dataclass
class DepositItem:
    date: datetime | None
    amount: Decimal
    currency: str
    is_refund: bool  # помечен как налоговый вычет (считается доходом, не взносом)


@dataclass
class ReturnInfo:
    account_name: str
    nav: Decimal            # текущая стоимость счёта
    contributed: Decimal    # свои взносы (без вычетов)
    withdrawn: Decimal      # выведено
    coupons: Decimal        # получено купонов
    dividends: Decimal      # получено дивидендов
    taxes: Decimal          # уплачено налогов (со знаком минус)
    tax_refund: Decimal     # налоговый вычет (засчитан как доход)
    profit: Decimal         # прибыль = nav + withdrawn - contributed
    xirr_pct: float | None  # годовая доходность (XIRR), %; None если не считается
    since: datetime | None  # дата первой операции
    currency: str


@dataclass
class BondCandidate:
    name: str
    ticker: str
    figi: str
    price_pct: Decimal        # чистая цена, % от номинала
    dirty_price: Decimal      # полная цена одной бумаги в рублях (с НКД)
    nominal: Decimal
    ytm_pct: float            # доходность к погашению, % годовых
    coupon_annual_pct: float | None
    maturity: datetime | None
    days_left: int
    is_ofz: bool
    lot: int = 1              # бумаг в одном лоте (у облигаций обычно 1)


@dataclass
class PlanItem:
    candidate: BondCandidate
    lots: int                 # сколько ЛОТОВ купить
    cost: Decimal             # полная стоимость (lots * lot * dirty_price)


@dataclass
class StockFund:
    ticker: str
    name: str
    figi: str
    pe: float | None                 # P/E (peRatioTtm)
    growth: float | None             # рост выручки (3y, как отдаёт API)
    debt_to_equity: float | None     # долг/капитал (totalDebtToEquityMrq)
    roe: float | None                # ROE
    div_yield: float | None          # дивдоходность (dividendYieldDailyTtm)
    market_cap: float | None
    free_float: float | None


class TInvestError(RuntimeError):
    pass


def _session_with(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "accept": "application/json",
    })
    return s


class TInvestClient:
    def __init__(self, token: str, account_selector: str = "iis",
                 trade_token: str = ""):
        self._token = token
        self._selector = account_selector
        self._session = _session_with(token)
        # торговый токен: отдельная сессия, если задан; иначе — основной токен
        self._trade_session = _session_with(trade_token) if trade_token else self._session

    # --- низкоуровневый вызов с перебором хостов ---
    def _call(self, service: str, method: str, payload: dict,
              trade: bool = False) -> dict:
        session = self._trade_session if trade else self._session
        url_path = f"/{_CONTRACT}.{service}/{method}"
        last_err: Exception | None = None
        for host in API_HOSTS:
            try:
                r = session.post(host + url_path, json=payload, timeout=30)
            except requests.RequestException as e:
                last_err = e
                log.warning("Хост %s недоступен (%s), пробую следующий", host, e)
                continue
            if r.status_code == 401:
                raise TInvestError("Токен недействителен (401). Проверь токен в .env.")
            if r.status_code == 429:
                raise TInvestError("Слишком много запросов (429). Подожди минуту.")
            if r.status_code == 403 or (not r.ok and "30079" in r.text):
                raise TInvestError(
                    "Недостаточно прав у токена (нужен full-access для покупок). "
                    "Проверь TINVEST_TRADE_TOKEN / TINVEST_TOKEN.")
            if not r.ok:
                # тело ошибки REST: {"code":..,"message":..,"description":..}
                msg = _err_text(r)
                raise TInvestError(f"{method}: HTTP {r.status_code} — {msg}")
            return r.json()
        raise TInvestError(f"Не удалось соединиться с API T-Invest: {last_err}")

    # --- счета ---
    def _accounts(self) -> list[dict]:
        data = self._call("UsersService", "GetAccounts", {})
        return data.get("accounts", [])

    def resolve_account(self) -> tuple[str, str]:
        accounts = self._accounts()
        if not accounts:
            raise TInvestError("У токена нет доступных счетов.")

        # прямое совпадение по id
        for a in accounts:
            if a.get("id") == self._selector:
                return a["id"], a.get("name") or a["id"]

        wanted = _SELECTOR_TO_TYPE.get(self._selector.lower())
        if wanted:
            # среди подходящих по типу — сначала открытый
            matches = [a for a in accounts if a.get("type") == wanted]
            matches.sort(key=lambda a: a.get("status") != "ACCOUNT_STATUS_OPEN")
            if matches:
                a = matches[0]
                return a["id"], a.get("name") or _TYPE_HUMAN.get(wanted, wanted)
            avail = ", ".join(
                f"{a.get('name') or a.get('id')} [{_TYPE_HUMAN.get(a.get('type'), a.get('type'))}]"
                for a in accounts
            )
            raise TInvestError(f"Счёт типа '{self._selector}' не найден. Доступные: {avail}")

        a = accounts[0]
        return a["id"], a.get("name") or a["id"]

    def list_accounts(self) -> list[str]:
        out = []
        for a in self._accounts():
            t = _TYPE_HUMAN.get(a.get("type"), a.get("type"))
            st = a.get("status", "").replace("ACCOUNT_STATUS_", "").lower()
            out.append(f"{a.get('name') or a.get('id')} — id={a.get('id')} — {t} ({st})")
        return out

    # --- баланс ---
    def get_balance(self) -> Balance:
        acc_id, acc_name = self.resolve_account()
        p = self._call("OperationsService", "GetPortfolio",
                       {"accountId": acc_id, "currency": "RUB"})
        total = p.get("totalAmountPortfolio")
        return Balance(
            account_name=acc_name,
            account_id=acc_id,
            total=_money(total),
            currency=_ccy(total) or "RUB",
            bonds=_money(p.get("totalAmountBonds")),
            shares=_money(p.get("totalAmountShares")),
            etf=_money(p.get("totalAmountEtf")),
            money=_money(p.get("totalAmountCurrencies")),
            expected_yield_pct=_money(p.get("expectedYield")),
        )

    # --- позиции-облигации ---
    def _bond_positions(self, acc_id: str) -> list[dict]:
        p = self._call("OperationsService", "GetPortfolio",
                       {"accountId": acc_id, "currency": "RUB"})
        return [pos for pos in p.get("positions", [])
                if pos.get("instrumentType") == "bond"]

    def _bond_info(self, figi: str) -> dict:
        data = self._call("InstrumentsService", "BondBy",
                          {"idType": "INSTRUMENT_ID_TYPE_FIGI", "id": figi})
        return data.get("instrument", {})

    # --- ближайшие купоны ---
    def get_upcoming_coupons(self, lookahead_days: int,
                             hide_zero: bool = True) -> list[CouponItem]:
        now = datetime.now(timezone.utc)
        to = now + timedelta(days=lookahead_days)
        acc_id, _ = self.resolve_account()
        out: list[CouponItem] = []
        for pos in self._bond_positions(acc_id):
            figi = pos.get("figi")
            qty = _money(pos.get("quantity"))
            try:
                bond = self._bond_info(figi)
                events = self._call("InstrumentsService", "GetBondCoupons",
                                    {"instrumentId": figi,
                                     "from": _iso(now), "to": _iso(to)}).get("events", [])
            except TInvestError as e:
                log.warning("Купоны по %s недоступны: %s", figi, e)
                continue
            for ev in events:
                d = _dt(ev.get("couponDate"))
                if d and d >= now:
                    per = _money(ev.get("payOneBond"))
                    if hide_zero and per == 0:
                        continue  # сумма купона ещё не определена (плавающий)
                    out.append(CouponItem(
                        bond_name=bond.get("name", figi),
                        ticker=bond.get("ticker", ""),
                        date=d,
                        per_bond=per,
                        quantity=qty,
                        total=per * qty,
                        currency=_ccy(ev.get("payOneBond")) or "RUB",
                    ))
        out.sort(key=lambda c: c.date)
        return out

    # --- сроки погашения ---
    def get_maturities(self) -> list[MaturityItem]:
        now = datetime.now(timezone.utc)
        acc_id, _ = self.resolve_account()
        out: list[MaturityItem] = []
        for pos in self._bond_positions(acc_id):
            figi = pos.get("figi")
            qty = _money(pos.get("quantity"))
            try:
                bond = self._bond_info(figi)
            except TInvestError as e:
                log.warning("Инструмент %s недоступен: %s", figi, e)
                continue
            mat = _dt(bond.get("maturityDate"))
            days_left = (mat - now).days if mat else -1
            out.append(MaturityItem(
                bond_name=bond.get("name", figi),
                ticker=bond.get("ticker", ""),
                maturity=mat,
                quantity=qty,
                nominal=_money(bond.get("nominal")),
                currency=_ccy(bond.get("nominal")) or "RUB",
                days_left=days_left,
            ))
        out.sort(key=lambda m: (m.maturity is None, m.maturity or now))
        return out

    # --- история операций (пагинация) ---
    def _all_operations(self, acc_id: str) -> list[dict]:
        frm = _iso(datetime(2015, 1, 1, tzinfo=timezone.utc))
        to = _iso(datetime.now(timezone.utc))
        cursor = ""
        out: list[dict] = []
        for _ in range(200):  # предохранитель от бесконечной пагинации
            payload = {"accountId": acc_id, "from": frm, "to": to,
                       "limit": 1000, "state": "OPERATION_STATE_EXECUTED"}
            if cursor:
                payload["cursor"] = cursor
            data = self._call("OperationsService", "GetOperationsByCursor", payload)
            items = data.get("items") or data.get("operations") or []
            out.extend(items)
            has_next = data.get("hasNext")
            nxt = data.get("nextCursor") or data.get("cursor")
            if has_next and nxt:
                cursor = nxt
            else:
                break
        return out

    # --- список пополнений (для поиска вычета) ---
    def get_deposits(self, tax_refund_amounts=None) -> list[DepositItem]:
        refunds = [Decimal(str(a)) for a in (tax_refund_amounts or [])]
        acc_id, _ = self.resolve_account()
        out: list[DepositItem] = []
        for op in self._all_operations(acc_id):
            if _op_type(op) == "OPERATION_TYPE_INPUT":
                amt = _money(op.get("payment"))
                out.append(DepositItem(
                    date=_dt(op.get("date")),
                    amount=amt,
                    currency=_ccy(op.get("payment")) or "RUB",
                    is_refund=_matches_refund(amt, refunds),
                ))
        out.sort(key=lambda d: d.date or datetime.now(timezone.utc))
        return out

    # --- годовая доходность (XIRR) + разбивка ---
    def compute_return(self, tax_refund_amounts=None) -> ReturnInfo:
        refunds = [Decimal(str(a)) for a in (tax_refund_amounts or [])]
        acc_id, acc_name = self.resolve_account()
        p = self._call("OperationsService", "GetPortfolio",
                       {"accountId": acc_id, "currency": "RUB"})
        nav = _money(p.get("totalAmountPortfolio"))
        nav_ccy = _ccy(p.get("totalAmountPortfolio")) or "RUB"

        contributed = withdrawn = coupons = dividends = taxes = refund = Decimal(0)
        flows: list[tuple[datetime, float]] = []
        dates: list[datetime] = []
        for op in self._all_operations(acc_id):
            t = _op_type(op)
            pay = _money(op.get("payment"))
            d = _dt(op.get("date"))
            if d:
                dates.append(d)
            if t == "OPERATION_TYPE_INPUT":
                if _matches_refund(pay, refunds):
                    refund += pay                      # вычет — это доход, не взнос
                else:
                    contributed += pay
                    if d:
                        flows.append((d, float(-pay)))  # деньги вложены → минус
            elif t == "OPERATION_TYPE_OUTPUT":
                withdrawn += -pay                       # pay отрицателен
                if d:
                    flows.append((d, float(-pay)))      # деньги вернулись → плюс
            elif t == "OPERATION_TYPE_COUPON":
                coupons += pay
            elif t == "OPERATION_TYPE_DIVIDEND":
                dividends += pay
            elif t in ("OPERATION_TYPE_TAX", "OPERATION_TYPE_BOND_TAX",
                       "OPERATION_TYPE_DIVIDEND_TAX", "OPERATION_TYPE_TAX_CORRECTION",
                       "OPERATION_TYPE_BENEFIT_TAX"):
                taxes += pay                            # отрицательно

        now = datetime.now(timezone.utc)
        if nav > 0:
            flows.append((now, float(nav)))            # финальная стоимость
        xirr = _xirr(flows)
        profit = nav + withdrawn - contributed
        return ReturnInfo(
            account_name=acc_name, nav=nav, contributed=contributed,
            withdrawn=withdrawn, coupons=coupons, dividends=dividends,
            taxes=taxes, tax_refund=refund, profit=profit,
            xirr_pct=(xirr * 100 if xirr is not None else None),
            since=(min(dates) if dates else None), currency=nav_ccy,
        )

    # ================= ОТБОР ОБЛИГАЦИЙ (реинвест) =================

    def _all_bonds(self) -> list[dict]:
        data = self._call("InstrumentsService", "Bonds",
                          {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})
        return data.get("instruments", [])

    def _last_prices(self, figis: list[str]) -> dict[str, Decimal]:
        """Последние цены (для облигаций — в % от номинала). {figi: price_pct}."""
        out: dict[str, Decimal] = {}
        for i in range(0, len(figis), 300):  # разумный размер батча
            chunk = figis[i:i + 300]
            data = self._call("MarketDataService", "GetLastPrices",
                              {"instrumentId": chunk})
            for lp in data.get("lastPrices", []):
                out[lp.get("figi")] = _money(lp.get("price"))
        return out

    def get_reinvest_candidates(self, min_days: int, max_days: int,
                                whitelist: list[str], max_screened: int = 80
                                ) -> list[BondCandidate]:
        now = datetime.now(timezone.utc)
        wl = [w.lower() for w in whitelist]
        prelim: list[dict] = []
        for b in self._all_bonds():
            if _bond_passes(b, now, min_days, max_days, wl):
                prelim.append(b)
        # ОФЗ первыми, затем корпораты; ограничиваем число расчётов YTM
        prelim.sort(key=lambda b: (not _is_ofz(b)))
        truncated = len(prelim) > max_screened
        prelim = prelim[:max_screened]

        figis = [b.get("figi") for b in prelim if b.get("figi")]
        prices = self._last_prices(figis)

        out: list[BondCandidate] = []
        for b in prelim:
            figi = b.get("figi")
            price_pct = prices.get(figi)
            if not price_pct or price_pct <= 0:
                continue  # нет актуальной котировки — пропускаем (ликвидность)
            nominal = _money(b.get("nominal"))
            aci = _money(b.get("aciValue"))
            mat = _dt(b.get("maturityDate"))
            if nominal <= 0 or mat is None:
                continue
            clean = price_pct / Decimal(100) * nominal
            dirty = clean + aci
            try:
                events = self._call("InstrumentsService", "GetBondCoupons",
                                    {"instrumentId": figi, "from": _iso(now),
                                     "to": _iso(mat + timedelta(days=1))}).get("events", [])
            except TInvestError as e:
                log.warning("Купоны по %s недоступны: %s", figi, e)
                continue
            flows: list[tuple[datetime, float]] = [(now, float(-dirty))]
            next_coupon = None
            for ev in events:
                cd = _dt(ev.get("couponDate"))
                pay = _money(ev.get("payOneBond"))
                if cd and cd > now and pay > 0:
                    flows.append((cd, float(pay)))
                    if next_coupon is None:
                        next_coupon = pay
            flows.append((mat, float(nominal)))
            ytm = _xirr(flows)
            if ytm is None:
                continue
            cqy = b.get("couponQuantityPerYear") or 0
            coupon_annual = (float(next_coupon) * float(cqy) / float(nominal) * 100
                             if next_coupon and nominal and cqy else None)
            out.append(BondCandidate(
                name=b.get("name", figi), ticker=b.get("ticker", ""), figi=figi,
                price_pct=price_pct, dirty_price=dirty, nominal=nominal,
                ytm_pct=ytm * 100, coupon_annual_pct=coupon_annual,
                maturity=mat, days_left=(mat - now).days, is_ofz=_is_ofz(b),
                lot=int(b.get("lot") or 1),
            ))
        out.sort(key=lambda c: c.ytm_pct, reverse=True)
        if truncated:
            log.info("Отбор ограничен %d бумагами (bond_max_screened).", max_screened)
        return out

    def market_is_open(self, figi: str) -> bool | None:
        """Идут ли сейчас нормальные торги по бумаге (для пометки о свежести цен)."""
        try:
            d = self._call("MarketDataService", "GetTradingStatus",
                           {"instrumentId": figi})
        except TInvestError:
            return None
        return d.get("tradingStatus") == "SECURITY_TRADING_STATUS_NORMAL_TRADING"

    # --- план покупки (жадное распределение по лотам) ---
    @staticmethod
    def plan_purchase(candidates: list[BondCandidate], free_cash: Decimal,
                      top_n: int, max_rub: float = 0.0) -> tuple[list[PlanItem], Decimal]:
        top = candidates[:top_n]
        budget = free_cash
        if max_rub and max_rub > 0:
            budget = min(budget, Decimal(str(max_rub)))
        lots = {c.figi: 0 for c in top}
        unit = {c.figi: (c.dirty_price * c.lot) for c in top}  # цена одного лота
        left = budget
        for c in top:                       # по 1 лоту каждой (диверсификация)
            if unit[c.figi] > 0 and left >= unit[c.figi]:
                lots[c.figi] += 1
                left -= unit[c.figi]
        filling = True
        while filling:                      # добираем самую доходную из доступных
            filling = False
            for c in top:
                if unit[c.figi] > 0 and left >= unit[c.figi]:
                    lots[c.figi] += 1
                    left -= unit[c.figi]
                    filling = True
                    break
        items, spent = [], Decimal(0)
        for c in top:                        # включаем все top (в т.ч. с 0 лотов)
            cost = lots[c.figi] * unit[c.figi]
            items.append(PlanItem(c, lots[c.figi], cost))
            spent += cost
        return items, free_cash - spent

    # --- выставление рыночной заявки на покупку (Этап C) ---
    def post_market_buy(self, account_id: str, figi: str, lots: int,
                        order_id: str) -> dict:
        """Рыночная заявка на покупку. Требует full-access токен."""
        return self._call("OrdersService", "PostOrder", {
            "accountId": account_id,
            "instrumentId": figi,
            "quantity": str(lots),
            "direction": "ORDER_DIRECTION_BUY",
            "orderType": "ORDER_TYPE_MARKET",
            "orderId": order_id,
        }, trade=True)

    def trade_account_id(self) -> str:
        acc_id, _ = self.resolve_account()
        return acc_id

    # ================= АКЦИИ (фундаментал) =================
    def _all_shares(self) -> list[dict]:
        return self._call("InstrumentsService", "Shares",
                          {"instrumentStatus": "INSTRUMENT_STATUS_BASE"}
                          ).get("instruments", [])

    def get_stock_fundamentals(self, whitelist_tickers: list[str]) -> list[StockFund]:
        wl = {t.upper() for t in whitelist_tickers}
        picked = [s for s in self._all_shares()
                  if (s.get("ticker") or "").upper() in wl
                  and (s.get("currency") or "").lower() == "rub"]

        def auid(s):
            return s.get("assetUid") or s.get("uid")

        ids = [auid(s) for s in picked if auid(s)]
        funds: dict[str, dict] = {}
        for i in range(0, len(ids), 90):
            try:
                resp = self._call("InstrumentsService", "GetAssetFundamentals",
                                  {"assets": ids[i:i + 90]})
            except TInvestError as e:
                log.warning("GetAssetFundamentals недоступен: %s", e)
                continue
            for f in resp.get("fundamentals", []):
                funds[f.get("assetUid")] = f

        out: list[StockFund] = []
        for s in picked:
            f = funds.get(auid(s))
            if not f:
                continue
            out.append(StockFund(
                ticker=(s.get("ticker") or "").upper(),
                name=s.get("name") or s.get("ticker") or "",
                figi=s.get("figi") or "",
                pe=_f(f.get("peRatioTtm")),
                growth=_f(f.get("threeYearAnnualRevenueGrowthRate"),
                          f.get("oneYearAnnualRevenueGrowthRate")),
                debt_to_equity=_f(f.get("totalDebtToEquityMrq")),
                roe=_f(f.get("roe")),
                div_yield=_f(f.get("dividendYieldDailyTtm")),
                market_cap=_f(f.get("marketCapitalization")),
                free_float=_f(f.get("freeFloat")),
            ))
        return out

    def reference_bond_ytm(self, min_days, max_days, whitelist, max_screened):
        """Ставка сравнения для акций = YTM лучшей надёжной облигации."""
        cands = self.get_reinvest_candidates(min_days, max_days, whitelist, max_screened)
        return cands[0].ytm_pct if cands else None

    # --- проверка соединения ---
    def check(self) -> str:
        acc_id, acc_name = self.resolve_account()
        return f"OK: счёт '{acc_name}' (id={acc_id})"


def _err_text(r: requests.Response) -> str:
    try:
        j = r.json()
        return j.get("message") or j.get("description") or r.text[:200]
    except Exception:
        return r.text[:200]


def _f(*vals):
    """Первое непустое значение, приведённое к float (или None)."""
    for v in vals:
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _op_type(op: dict) -> str:
    """Достать enum типа операции независимо от имени поля."""
    for key in ("type", "operationType"):
        v = op.get(key, "")
        if isinstance(v, str) and v.startswith("OPERATION_TYPE"):
            return v
    return ""


def _matches_refund(amount: Decimal, refunds: list[Decimal]) -> bool:
    """Совпадает ли пополнение с суммой налогового вычета (±1 руб)."""
    return any(abs(amount - r) <= Decimal("1") for r in refunds)


def _is_ofz(bond: dict) -> bool:
    name = (bond.get("name") or "").lower()
    ticker = (bond.get("ticker") or "").upper()
    return name.startswith("офз") or ticker.startswith("SU")


def _bond_reliable(bond: dict, whitelist: list[str]) -> bool:
    if _is_ofz(bond):
        return True
    name = (bond.get("name") or "").lower()
    return any(frag in name for frag in whitelist)


def _bond_passes(bond: dict, now: datetime, min_days: int, max_days: int,
                 whitelist: list[str]) -> bool:
    if (bond.get("currency") or "").lower() != "rub":
        return False
    if bond.get("floatingCouponFlag") or bond.get("perpetualFlag") \
            or bond.get("amortizationFlag"):
        return False
    mat = _dt(bond.get("maturityDate"))
    if mat is None:
        return False
    days = (mat - now).days
    if days < min_days or days > max_days:
        return False
    if not _bond_reliable(bond, whitelist):
        return False
    # корпоратам требуем низкий уровень риска; ОФЗ пропускаем всегда
    if not _is_ofz(bond) and bond.get("riskLevel") != "RISK_LEVEL_LOW":
        return False
    return True


def _xirr(flows: list[tuple[datetime, float]]) -> float | None:
    """Годовая доходность по денежным потокам (метод бисекции).

    flows: список (дата, сумма), где вложения — отрицательные, поступления и
    финальная стоимость — положительные. Возвращает ставку (доля/год) или None.
    """
    flows = [f for f in flows if f[1] != 0]
    if len(flows) < 2:
        return None
    t0 = min(f[0] for f in flows)

    def npv(rate: float) -> float:
        acc = 0.0
        for t, cf in flows:
            years = (t - t0).days / 365.0
            acc += cf / (1.0 + rate) ** years
        return acc

    lo, hi = -0.9999, 1.0
    f_lo = npv(lo)
    f_hi = npv(hi)
    # расширяем верхнюю границу, пока не поймаем смену знака
    tries = 0
    while f_lo * f_hi > 0 and tries < 12:
        hi *= 2.0
        f_hi = npv(hi)
        tries += 1
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0
