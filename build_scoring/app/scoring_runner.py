"""Обёртка отдельного скоринг-аддона (read-only).

Ответственность обёртки (пакет tinvest_scoring формулы НЕ трогаем):
  1. Планировщик внутри контейнера (07:00 Europe/Moscow + повторы 07:30/08:00),
     AS_OF = вчерашняя календарная дата по Москве; НЕ cron HAOS.
  2. Проверка свежести двух выгрузок бота из /share (read-only).
  3. Изолированный запуск CLI пакета: iis-candidates → market-refresh.
  4. Валидация результата перед отправкой (схема/методология/AS_OF/строки/
     unresolved_identities/NaN/размер).
  5. Публикация scoring_snapshot_v03.json в приёмник витрины /scoring-ingest
     (только если publish_enabled; повтор тем же телом; при неуспехе прошлый
     снимок витрины не трогаем; generated_at не подменяем).

ГРАНИЦЫ: токена Т-Банка нет; /share только для чтения; секрет не логируется и
НЕ передаётся дочернему расчётному процессу; торговый контур не затрагивается.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ------------------------------- пути/константы -----------------------------

SHARE_EXPORT = Path("/share/tinvest/export")
UNIVERSE_SRC = SHARE_EXPORT / "tbank_iis_universe.csv"
ALLOWLIST_SRC = SHARE_EXPORT / "iis_allowlist_current.csv"
QUALITY_SRC = Path("/share/tinvest/scoring/quality/quality_snapshot.json")

DATA = Path("/data/scoring")
RUNS = DATA / "runs"
PENDING = DATA / "pending"
STATE_FILE = DATA / "state.json"
LAST_GOOD = DATA / "last_scoring_snapshot.json"

METHODOLOGY = Path(os.getenv("SCORING_METHODOLOGY", "/opt/scoring/methodology-v0.3.md"))
VENV_PY = os.getenv("SCORING_VENV", "/opt/scoring-venv") + "/bin/python"
CLI_MODULE = "tinvest_scoring"

SCHEMA_VERSION = "scoring_snapshot.v0.3"
METHODOLOGY_VERSION = "0.3"
MAX_BYTES = 512 * 1024
KEEP_RUNS = 14                       # ротация рабочих каталогов
SECRET_ENV = "SCORING_SERVICE_SECRET"

log = logging.getLogger("scoring")


# ------------------------------- конфиг из env ------------------------------

def _bool(v: str | None) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self, env=None):
        e = env if env is not None else os.environ
        self.publish_enabled = _bool(e.get("PUBLISH_ENABLED"))
        self.run_on_start = _bool(e.get("RUN_ON_START"))
        self.ingest_url = (e.get("SCORING_INGEST_URL") or "").strip()
        self.tz = ZoneInfo(e.get("SCORING_TZ") or "Europe/Moscow")
        self.schedule_time = (e.get("SCHEDULE_TIME") or "07:00").strip()
        self.retry_times = [t.strip() for t in (e.get("RETRY_TIMES") or "").split(",") if t.strip()]
        self.workers = int(e.get("WORKERS") or "2")
        self.max_age_hours = int(e.get("INPUT_MAX_AGE_HOURS") or "36")
        self.use_quality = _bool(e.get("USE_QUALITY_SNAPSHOT"))
        self.attempt_timeout = int(e.get("ATTEMPT_TIMEOUT_MINUTES") or "30") * 60
        self.log_level = (e.get("LOG_LEVEL") or "info").upper()

    @property
    def secret(self) -> str:
        # Читаем секрет только в момент использования, наружу не отдаём.
        return os.environ.get(SECRET_ENV, "")

    @property
    def fire_times(self) -> list[str]:
        seen, out = set(), []
        for t in [self.schedule_time, *self.retry_times]:
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out


# ------------------------- чистые функции (тестируемые) ---------------------

def as_of_for_day(day) -> str:
    """AS_OF запуска в день D — предыдущая календарная дата (D-1)."""
    return (day - timedelta(days=1)).isoformat()


def most_recent_expected_as_of(now, primary_time: str) -> str:
    """AS_OF последнего наступившего основного запуска (для догоняющего старта)."""
    hh, mm = (int(x) for x in primary_time.split(":"))
    primary_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    base_day = now.date() if now >= primary_today else now.date() - timedelta(days=1)
    return as_of_for_day(base_day)


def compute_next_fire(now, fire_times: list[str], published: set[str]):
    """Ближайший момент запуска среди дневных времён, чей AS_OF ещё НЕ опубликован.

    Повторы (07:30/08:00) срабатывают, только пока AS_OF дня не закрыт: как
    только он в published, кандидаты этого дня пропускаются → следующий день.
    """
    times = []
    for t in fire_times:
        hh, mm = (int(x) for x in t.split(":"))
        times.append((hh, mm))
    for add in range(0, 8):                       # смотрим на неделю вперёд максимум
        day = now.date() + timedelta(days=add)
        for hh, mm in sorted(times):
            cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=now.tzinfo)
            if cand <= now:
                continue
            if as_of_for_day(day) in published:
                continue
            return cand
    return now + timedelta(hours=1)                # страховка, не должно случаться


def parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    txt = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        # только дата
        try:
            dt = datetime.fromisoformat(txt + "T00:00:00+00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_freshness(ts_str, now_utc, max_age_hours: int):
    """(ok, reason). Пустая/будущая/слишком старая дата — ошибка входа."""
    dt = parse_iso(ts_str)
    if dt is None:
        return False, f"нераспознанная дата '{ts_str}'"
    if dt > now_utc + timedelta(minutes=5):
        return False, f"дата в будущем {ts_str}"
    if now_utc - dt > timedelta(hours=max_age_hours):
        return False, f"старше {max_age_hours}ч ({ts_str})"
    return True, ""


def _reject_nan(_v):
    raise ValueError("NaN/Infinity в JSON запрещены")


def _find_value(obj, key):
    """Первое значение по ключу в произвольно вложенной структуре или None."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def validate_snapshot(obj, expected_as_of):
    """Лёгкая проверка перед POST. Строгую делает Worker витрины."""
    if not isinstance(obj, dict):
        return False, "снимок не объект"
    if obj.get("schema_version") != SCHEMA_VERSION:
        return False, f"schema_version={obj.get('schema_version')!r}"
    if str(obj.get("methodology_version")) != METHODOLOGY_VERSION:
        return False, f"methodology_version={obj.get('methodology_version')!r}"
    if expected_as_of and obj.get("as_of") != expected_as_of:
        return False, f"as_of={obj.get('as_of')!r} != {expected_as_of}"
    rows = obj.get("rows")
    if not isinstance(rows, list) or not rows:
        return False, "пустой rows"
    unresolved = _find_value(obj, "unresolved_identities")
    if unresolved not in (None, 0):
        return False, f"unresolved_identities={unresolved}"
    return True, ""


def categorize_publish(status: int) -> str:
    """OK | TERMINAL | RETRY по HTTP-коду приёмника (контракт /scoring-ingest)."""
    if status in (200, 202):
        return "OK"
    if status in (401, 409, 413, 415, 422):
        return "TERMINAL"
    if status == 429 or 500 <= status < 600:
        return "RETRY"
    return "TERMINAL"                              # 3xx и прочее — неожиданно


# ------------------------------- состояние ----------------------------------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"published_as_of": [], "last_generated_at": ""}


def save_state(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def record_as_of(state: dict, as_of: str, generated_at: str = "") -> None:
    lst = state.setdefault("published_as_of", [])
    if as_of not in lst:
        lst.append(as_of)
    state["published_as_of"] = lst[-30:]
    if generated_at:
        state["last_generated_at"] = generated_at
    save_state(state)


# ------------------------------- ввод/вывод ---------------------------------

def read_csv_meta(path: Path, ts_col: str):
    """(ts_value, n_rows) по имени колонки времени; (None, 0) если нет файла."""
    import csv
    if not path.exists():
        return None, 0
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        try:
            header = next(r)
        except StopIteration:
            return None, 0
        idx = header.index(ts_col) if ts_col in header else -1
        ts, n = None, 0
        for row in r:
            n += 1
            if ts is None and idx >= 0 and idx < len(row):
                ts = row[idx]
    return ts, n


def child_env() -> dict:
    """Среда для расчётного subprocess — БЕЗ сервисного секрета."""
    return {k: v for k, v in os.environ.items() if k != SECRET_ENV}


def run_cli(args: list[str], timeout: int) -> int:
    cmd = [VENV_PY, "-m", CLI_MODULE, *args]
    log.info("CLI: %s", " ".join(a for a in cmd if not a.startswith("/data")))
    try:
        p = subprocess.run(cmd, env=child_env(), timeout=timeout,
                           capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        log.error("CLI превысил лимит %ss — расчёт остановлен", timeout)
        return 124
    if p.returncode != 0:
        log.error("CLI rc=%s: %s", p.returncode, (p.stderr or "")[-500:])
    return p.returncode


def rotate_runs(keep: int = KEEP_RUNS) -> None:
    try:
        dirs = sorted([d for d in RUNS.iterdir() if d.is_dir()])
    except FileNotFoundError:
        return
    for d in dirs[:-keep]:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------- публикация ---------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Не пересылаем Authorization при редиректах: редирект приёмника — ошибка.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def publish(body: bytes, cfg: Config):
    """(category, status, result_text). Секрет и заголовки НЕ логируем."""
    req = urllib.request.Request(
        cfg.ingest_url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.secret}"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as resp:
            status = resp.status
            text = resp.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        text = (e.read(2048).decode("utf-8", "replace") if e.fp else "")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("publish сеть/таймаут: %s", str(e)[:200])
        return "RETRY", 0, "network_error"
    result = ""
    try:
        b = json.loads(text)
        if isinstance(b, dict):
            result = str(b.get("status") or b.get("error") or "")
    except Exception:  # noqa: BLE001
        pass
    return categorize_publish(status), status, result


# ------------------------------- один запуск --------------------------------

def _prepare_run_dir(as_of: str, cfg: Config):
    now = datetime.now(timezone.utc)
    run_dir = RUNS / f"{as_of}T{now:%H%M%S}Z"
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(UNIVERSE_SRC, run_dir / "tbank_iis_universe.csv")
    shutil.copy2(ALLOWLIST_SRC, run_dir / "iis_allowlist_current.csv")
    use_quality = cfg.use_quality and QUALITY_SRC.exists()
    if use_quality:
        shutil.copy2(QUALITY_SRC, run_dir / "quality_snapshot.json")
    return run_dir, use_quality


def compute(as_of: str, cfg: Config) -> bytes | None:
    """Свежесть → копия входов → iis-candidates → market-refresh → валидация.
    Возвращает байты снимка или None (тогда запуск повторится по расписанию)."""
    now_utc = datetime.now(timezone.utc)
    uni_ts, uni_n = read_csv_meta(UNIVERSE_SRC, "generated_at")
    alw_ts, alw_n = read_csv_meta(ALLOWLIST_SRC, "observed_at")
    if not uni_n or not alw_n:
        log.error("Нет входов: universe=%s строк, allowlist=%s строк", uni_n, alw_n)
        return None
    for name, ts in (("universe", uni_ts), ("allowlist", alw_ts)):
        ok, why = check_freshness(ts, now_utc, cfg.max_age_hours)
        if not ok:
            log.error("Вход %s несвежий: %s", name, why)
            return None

    run_dir, use_quality = _prepare_run_dir(as_of, cfg)
    cand = run_dir / "iis_share_candidates.csv"
    rc = run_cli(["iis-candidates", "--as-of", as_of,
                  "--tbank-universe", str(run_dir / "tbank_iis_universe.csv"),
                  "--allowlist", str(run_dir / "iis_allowlist_current.csv"),
                  "--output", str(cand)], cfg.attempt_timeout)
    if rc != 0 or not cand.exists():
        return None

    market = run_dir / "market"
    mr = ["market-refresh", "--as-of", as_of, "--base-precheck", str(cand),
          "--methodology", str(METHODOLOGY), "--workers", str(cfg.workers),
          "--output-dir", str(market)]
    if use_quality:
        mr += ["--quality-snapshot", str(run_dir / "quality_snapshot.json")]
    else:
        log.info("quality_snapshot отсутствует/выключен — market-only режим")
    if run_cli(mr, cfg.attempt_timeout) != 0:
        errf = market / "download-errors.json"
        if errf.exists():
            log.error("MOEX-загрузка с ошибками: %s", errf.read_text("utf-8")[:400])
        return None

    snap = market / "scoring_snapshot_v03.json"
    if not snap.exists():
        log.error("Нет выходного snapshot")
        return None
    raw = snap.read_bytes()
    if len(raw) > MAX_BYTES:
        log.error("snapshot %s байт > лимита %s", len(raw), MAX_BYTES)
        return None
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_nan)
    except ValueError as e:
        log.error("snapshot не валиден как JSON: %s", e)
        return None
    ok, why = validate_snapshot(obj, as_of)
    if not ok:
        log.error("snapshot не прошёл проверку: %s", why)
        return None
    log.info("snapshot готов: as_of=%s строк=%s секторов=%s байт=%s",
             as_of, len(obj.get("rows", [])),
             len(obj.get("sectors", []) or []), len(raw))
    return raw


def attempt(as_of: str, cfg: Config, state: dict) -> str:
    """DONE | RETRY_LATER. DONE фиксирует as_of (повторов в этот день не будет)."""
    PENDING.mkdir(parents=True, exist_ok=True)
    pending = PENDING / f"{as_of}.json"
    if pending.exists():
        body = pending.read_bytes()               # повтор тем же телом (без пересчёта)
        log.info("Повторная отправка того же снимка as_of=%s", as_of)
    else:
        body = compute(as_of, cfg)
        if body is None:
            return "RETRY_LATER"
        pending.write_bytes(body)

    gen_at = ""
    try:
        gen_at = str(json.loads(body).get("generated_at") or "")
    except Exception:  # noqa: BLE001
        pass

    if not cfg.publish_enabled:
        log.info("Сухой прогон (publish_enabled=false): снимок НЕ отправлен")
        record_as_of(state, as_of, gen_at)
        pending.unlink(missing_ok=True)
        rotate_runs()
        return "DONE"

    if not cfg.secret:
        log.error("Публикация включена, но секрет пуст — пропуск (проверь настройку)")
        record_as_of(state, as_of, gen_at)
        pending.unlink(missing_ok=True)
        return "DONE"

    cat, status, result = publish(body, cfg)
    log.info("scoring_ingest status=%s cat=%s result=%s as_of=%s schema=%s",
             status, cat, result or "-", as_of, SCHEMA_VERSION)
    if cat == "OK":
        LAST_GOOD.write_bytes(body)
        record_as_of(state, as_of, gen_at)
        pending.unlink(missing_ok=True)
        rotate_runs()
        return "DONE"
    if cat == "TERMINAL":
        log.error("Публикация отклонена (без повтора): status=%s result=%s", status, result)
        record_as_of(state, as_of, gen_at)          # не крутим повторы в этот день
        pending.unlink(missing_ok=True)
        return "DONE"
    return "RETRY_LATER"                             # RETRY: тело сохранено, повтор позже


# ------------------------------- главный цикл -------------------------------

def main() -> None:
    cfg = Config()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    DATA.mkdir(parents=True, exist_ok=True)
    log.info("Скоринг-аддон: publish=%s расписание=%s(+%s) MSK quality=%s",
             cfg.publish_enabled, cfg.schedule_time,
             ",".join(cfg.retry_times) or "—", cfg.use_quality)
    state = load_state()

    # Догоняющий старт: один запуск за последнюю ожидаемую дату, если не закрыта.
    now = datetime.now(cfg.tz)
    expected = most_recent_expected_as_of(now, cfg.schedule_time)
    if cfg.run_on_start or expected not in set(state.get("published_as_of", [])):
        log.info("Старт: запуск за AS_OF=%s", expected)
        if attempt(expected, cfg, state) == "RETRY_LATER":
            log.info("Стартовый запуск не завершён — повторю по расписанию")

    while True:
        now = datetime.now(cfg.tz)
        published = set(state.get("published_as_of", []))
        fire = compute_next_fire(now, cfg.fire_times, published)
        sleep_s = max(30, (fire - now).total_seconds())
        log.info("Следующий запуск: %s MSK (через %.0f мин)",
                 fire.strftime("%Y-%m-%d %H:%M"), sleep_s / 60)
        time.sleep(sleep_s)
        fire_day = datetime.now(cfg.tz).date()
        as_of = as_of_for_day(fire_day)
        if as_of in set(state.get("published_as_of", [])):
            continue
        log.info("Запуск по расписанию: AS_OF=%s", as_of)
        attempt(as_of, cfg, state)


if __name__ == "__main__":
    main()
