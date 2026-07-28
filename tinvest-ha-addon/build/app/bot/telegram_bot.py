"""Telegram-бот на raw Bot API (long-polling через requests).

Чтение — по командам. Покупка облигаций — по кнопке с ДВУМЯ подтверждениями,
только при включённом trade_enabled и открытой бирже. Доступ по whitelist chat_id.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import requests

from . import formatters as fmt
from . import stocks
from .config import Config, DATA_DIR
from .tinvest_client import TInvestClient

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"
ORDERS_LOG = DATA_DIR / "orders.log"


class TelegramBot:
    def __init__(self, cfg: Config, tinvest: TInvestClient):
        self.cfg = cfg
        self.tinvest = tinvest
        self.offset = 0
        self.session = requests.Session()
        self.proposals: dict[str, dict] = {}   # pid -> предложение на покупку

    # --- низкоуровневые вызовы ---
    def _call(self, method: str, **params):
        url = API.format(token=self.cfg.telegram_token, method=method)
        r = self.session.post(url, json=params, timeout=self.cfg.poll_timeout + 10)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            log.error("Telegram API error (%s): %s", method, data)
        return data

    def send(self, chat_id: int, text: str, buttons=None):
        chunks = list(_split(text, 4000))
        for i, chunk in enumerate(chunks):
            params = dict(chat_id=chat_id, text=chunk, parse_mode="HTML",
                          disable_web_page_preview=True)
            if buttons and i == len(chunks) - 1:  # кнопки — к последнему куску
                params["reply_markup"] = {"inline_keyboard": buttons}
            self._call("sendMessage", **params)

    def answer_cb(self, cb_id: str, text: str = ""):
        self._call("answerCallbackQuery", callback_query_id=cb_id, text=text)

    # --- доступ ---
    def _allowed(self, chat_id: int) -> bool:
        return not self.cfg.allowed_chat_ids or chat_id in self.cfg.allowed_chat_ids

    # ================= КОМАНДЫ =================
    def handle(self, chat_id: int, text: str):
        cmd = text.strip().split()[0].lstrip("/").split("@")[0].lower()
        try:
            if cmd in ("start", "help"):
                self.send(chat_id, fmt.HELP_TEXT)
            elif cmd == "balance":
                self.send(chat_id, fmt.format_balance(self.tinvest.get_balance()))
            elif cmd == "income":
                r = self.tinvest.compute_return(self.cfg.tax_refund_amounts)
                self.send(chat_id, fmt.format_return(r, self.cfg.timezone))
            elif cmd == "deposits":
                items = self.tinvest.get_deposits(self.cfg.tax_refund_amounts)
                self.send(chat_id, fmt.format_deposits(items, self.cfg.timezone))
            elif cmd == "coupons":
                items = self.tinvest.get_upcoming_coupons(
                    self.cfg.coupon_lookahead_days, hide_zero=self.cfg.hide_zero_coupons)
                self.send(chat_id, fmt.format_coupons(items, self.cfg.timezone,
                                                      self.cfg.coupon_lookahead_days))
            elif cmd == "maturity":
                items = self.tinvest.get_maturities()
                self.send(chat_id, fmt.format_maturities(items, self.cfg.timezone))
            elif cmd == "topbonds":
                self.send(chat_id, "Считаю доходности облигаций…")
                cands = self.tinvest.get_reinvest_candidates(
                    self.cfg.bond_min_maturity_days, self.cfg.bond_max_maturity_days,
                    self.cfg.bond_whitelist, self.cfg.bond_max_screened)
                self.send(chat_id, fmt.format_topbonds(
                    cands, self.cfg.topbonds_n, self.cfg.timezone))
            elif cmd == "topstocks":
                self._cmd_topstocks(chat_id)
            elif cmd == "reinvest":
                self._cmd_reinvest(chat_id)
            elif cmd == "accounts":
                accs = self.tinvest.list_accounts()
                self.send(chat_id, "Счета:\n" + "\n".join(fmt.esc(a) for a in accs))
            else:
                self.send(chat_id, "Неизвестная команда. /help")
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка обработки команды %s", cmd)
            self.send(chat_id, f"⚠️ Ошибка: {fmt.esc(e)}")

    def _cmd_topstocks(self, chat_id: int):
        self.send(chat_id, "Собираю фундаментал акций, это займёт до минуты…")
        funds = self.tinvest.get_stock_fundamentals(self.cfg.stock_whitelist)
        if not funds:
            self.send(chat_id, "Не удалось получить фундаментал по акциям "
                               "(проверь список тикеров / доступность данных).")
            return
        opinions = stocks.load_opinions(funds)
        bond_ytm = self.tinvest.reference_bond_ytm(
            self.cfg.bond_min_maturity_days, self.cfg.bond_max_maturity_days,
            self.cfg.bond_whitelist, self.cfg.bond_max_screened)
        weights = {"value": self.cfg.w_value, "quality": self.cfg.w_quality,
                   "growth": self.cfg.w_growth, "dividend": self.cfg.w_dividend,
                   "personal": self.cfg.w_personal}
        scored = stocks.score_stocks(
            funds, opinions, weights, bond_ytm,
            self.cfg.stock_min_market_cap, self.cfg.stock_min_free_float,
            self.cfg.stock_min_pe)
        stocks.write_scores_csv(scored)
        self.send(chat_id, fmt.format_topstocks(scored, self.cfg.stock_top_n, bond_ytm))

    def _cmd_reinvest(self, chat_id: int):
        self.send(chat_id, "Подбираю облигации, это займёт до минуты…")
        cands = self.tinvest.get_reinvest_candidates(
            self.cfg.bond_min_maturity_days, self.cfg.bond_max_maturity_days,
            self.cfg.bond_whitelist, self.cfg.bond_max_screened)
        free = self.tinvest.get_balance().money
        market_open = self.tinvest.market_is_open(cands[0].figi) if cands else None
        items, remaining = TInvestClient.plan_purchase(
            cands, free, self.cfg.bond_top_n, self.cfg.reinvest_max_rub)

        planned = free - remaining
        buyable = [it for it in items if it.lots > 0]
        can_buy = bool(self.cfg.trade_enabled) and market_open is True \
            and bool(buyable) and planned > 0

        buttons = None
        if can_buy:
            pid = uuid.uuid4().hex[:8]
            self.proposals[pid] = {
                "chat_id": chat_id, "status": "proposed",
                "acc_id": self.tinvest.trade_account_id(),
                "total": planned,
                "items": [(it.candidate.name, it.candidate.figi, it.lots, it.cost)
                          for it in buyable],
            }
            buttons = [[{"text": f"🛒 Купить · {fmt._num(planned)} ₽",
                         "callback_data": f"rv|propose|{pid}"}]]

        self.send(chat_id, fmt.format_reinvest(
            items, free, remaining, self.cfg.reinvest_min_cash,
            self.cfg.timezone, market_open, can_buy), buttons=buttons)

    # ================= КНОПКИ (callback) =================
    def handle_callback(self, cb: dict):
        cb_id = cb.get("id")
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        data = cb.get("data") or ""
        if chat_id is None:
            return
        if not self._allowed(chat_id):
            self.answer_cb(cb_id, "Доступ запрещён")
            return
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "rv":
            self.answer_cb(cb_id)
            return
        _, action, pid = parts
        p = self.proposals.get(pid)
        if not p or p["chat_id"] != chat_id:
            self.answer_cb(cb_id, "Предложение устарело — сделай /reinvest заново")
            return

        try:
            if action == "propose":
                self._cb_propose(cb_id, chat_id, pid, p)
            elif action == "confirm":
                self._cb_confirm(cb_id, chat_id, pid, p)
            elif action == "cancel":
                self._cb_cancel(cb_id, chat_id, pid, p)
            else:
                self.answer_cb(cb_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка callback %s", action)
            self.answer_cb(cb_id, "Ошибка")
            self.send(chat_id, f"⚠️ Ошибка: {fmt.esc(e)}")

    def _cb_propose(self, cb_id, chat_id, pid, p):
        if p["status"] != "proposed":
            self.answer_cb(cb_id, "Уже обрабатывается")
            return
        # повторно убеждаемся, что торги идут
        if not (p["items"] and self.tinvest.market_is_open(p["items"][0][1])):
            self.answer_cb(cb_id, "Биржа закрыта")
            self.send(chat_id, "Биржа закрыта — покупка недоступна. Попробуй в торговые часы.")
            return
        p["status"] = "confirming"
        self.answer_cb(cb_id)
        lines = ["❓ <b>Подтверди покупку</b>\n"]
        for name, figi, lots, cost in p["items"]:
            lines.append(f"• {fmt.esc(name)} — {lots} лот. ≈ {fmt._num(cost)} ₽")
        lines.append(f"\nИтого: <b>{fmt._num(p['total'])} ₽</b> рыночными заявками.")
        buttons = [[{"text": "✅ Подтвердить", "callback_data": f"rv|confirm|{pid}"},
                    {"text": "✖ Отмена", "callback_data": f"rv|cancel|{pid}"}]]
        self.send(chat_id, "\n".join(lines), buttons=buttons)

    def _cb_confirm(self, cb_id, chat_id, pid, p):
        if p["status"] != "confirming":
            self.answer_cb(cb_id, "Уже обработано или отменено")
            return
        p["status"] = "executing"       # блокируем двойной клик
        self.answer_cb(cb_id, "Отправляю заявки…")
        results = []
        for name, figi, lots, cost in p["items"]:
            order_id = uuid.uuid4().hex
            try:
                resp = self.tinvest.post_market_buy(p["acc_id"], figi, lots, order_id)
                results.append((name, lots, resp))
                _log_order(order_id, name, figi, lots,
                           resp.get("executionReportStatus", "?"))
            except Exception as e:  # noqa: BLE001
                results.append((name, lots, e))
                _log_order(order_id, name, figi, lots, f"ERROR:{e}")
        p["status"] = "done"
        self.send(chat_id, fmt.format_order_report(results))
        self.send(chat_id, "Готово. Обнови /balance и при желании /reinvest.")

    def _cb_cancel(self, cb_id, chat_id, pid, p):
        if p["status"] in ("done", "executing"):
            self.answer_cb(cb_id, "Уже обработано")
            return
        p["status"] = "cancelled"
        self.answer_cb(cb_id, "Отменено")
        self.send(chat_id, "Покупка отменена.")

    # ================= ОСНОВНОЙ ЦИКЛ =================
    def run(self):
        mode = "ТОРГОВЛЯ ВКЛ" if self.cfg.trade_enabled else "только чтение"
        log.info("Бот запущен (%s), ожидаю команды…", mode)
        while True:
            try:
                resp = self._call("getUpdates", offset=self.offset,
                                  timeout=self.cfg.poll_timeout,
                                  allowed_updates=["message", "callback_query"])
            except requests.RequestException as e:
                log.warning("Сеть недоступна, повтор через 5с: %s", e)
                time.sleep(5)
                continue

            for upd in resp.get("result", []):
                self.offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    self.handle_callback(upd["callback_query"])
                    continue
                msg = upd.get("message") or {}
                text = msg.get("text") or ""
                chat_id = (msg.get("chat") or {}).get("id")
                if chat_id is None or not text.startswith("/"):
                    continue
                if not self._allowed(chat_id):
                    log.warning("Отказано chat_id=%s", chat_id)
                    self.send(chat_id, f"Доступ запрещён. Ваш chat_id: {chat_id}")
                    continue
                self.handle(chat_id, text)


def _log_order(order_id, name, figi, lots, status):
    try:
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with open(ORDERS_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts}\t{order_id}\t{figi}\t{name}\t{lots}\t{status}\n")
    except Exception:  # noqa: BLE001
        log.exception("Не удалось записать orders.log")


def _split(text: str, limit: int):
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf:
        yield buf
