"""Оффлайн-тесты чистых функций обёртки скоринг-аддона (без сети, без пакета).

Запуск:  cd build_scoring/app && python -m pytest ../tests -q
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import scoring_runner as sr  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")


def test_as_of_is_previous_day():
    assert sr.as_of_for_day(date(2026, 8, 31)) == "2026-08-30"


def test_most_recent_expected_as_of_before_and_after_primary():
    # 06:00 MSK — основной запуск 07:00 ещё не наступил → берём позавчера
    before = datetime(2026, 8, 31, 6, 0, tzinfo=MSK)
    assert sr.most_recent_expected_as_of(before, "07:00") == "2026-08-29"
    # 08:00 MSK — сегодняшний 07:00 уже прошёл → вчера
    after = datetime(2026, 8, 31, 8, 0, tzinfo=MSK)
    assert sr.most_recent_expected_as_of(after, "07:00") == "2026-08-30"


def test_next_fire_primary_then_retries_then_next_day():
    fires = ["07:00", "07:30", "08:00"]
    # 06:00, ничего не опубликовано → сегодня 07:00
    now = datetime(2026, 8, 31, 6, 0, tzinfo=MSK)
    nf = sr.compute_next_fire(now, fires, set())
    assert (nf.hour, nf.minute, nf.day) == (7, 0, 31)
    # 07:10, день ещё не закрыт → повтор 07:30
    now = datetime(2026, 8, 31, 7, 10, tzinfo=MSK)
    nf = sr.compute_next_fire(now, fires, set())
    assert (nf.hour, nf.minute, nf.day) == (7, 30, 31)
    # 07:10, но AS_OF дня уже опубликован → следующий день 07:00
    nf = sr.compute_next_fire(now, fires, {"2026-08-30"})
    assert (nf.hour, nf.minute, nf.day) == (7, 0, 1)  # 1 сентября


def test_parse_iso_variants():
    assert sr.parse_iso("2026-08-30T10:00:00Z") is not None
    assert sr.parse_iso("2026-08-30") is not None
    assert sr.parse_iso("не дата") is None
    assert sr.parse_iso("") is None


def test_freshness_ok_future_stale():
    now = datetime.fromisoformat("2026-08-31T05:00:00+00:00")
    assert sr.check_freshness("2026-08-31T04:00:00Z", now, 36)[0] is True
    assert sr.check_freshness("2026-08-31T23:00:00Z", now, 36)[0] is False   # будущее
    assert sr.check_freshness("2026-08-01T00:00:00Z", now, 36)[0] is False   # старое
    assert sr.check_freshness("мусор", now, 36)[0] is False


def _good_snapshot():
    return {
        "schema_version": "scoring_snapshot.v0.3",
        "methodology_version": "0.3",
        "as_of": "2026-08-30",
        "rows": [{"secid": "SBER", "market": {}, "quality": None}],
        "sectors": [{"sector": "financial"}],
        "summary": {"unresolved_identities": 0},
    }


def test_validate_snapshot_ok_and_failures():
    assert sr.validate_snapshot(_good_snapshot(), "2026-08-30")[0] is True
    bad = _good_snapshot(); bad["schema_version"] = "x"
    assert sr.validate_snapshot(bad, "2026-08-30")[0] is False
    bad = _good_snapshot(); bad["methodology_version"] = "0.2"
    assert sr.validate_snapshot(bad, "2026-08-30")[0] is False
    bad = _good_snapshot()
    assert sr.validate_snapshot(bad, "2026-08-31")[0] is False   # as_of mismatch
    bad = _good_snapshot(); bad["rows"] = []
    assert sr.validate_snapshot(bad, "2026-08-30")[0] is False
    bad = _good_snapshot(); bad["summary"]["unresolved_identities"] = 3
    assert sr.validate_snapshot(bad, "2026-08-30")[0] is False


def test_find_value_nested():
    assert sr._find_value({"a": {"b": {"unresolved_identities": 5}}},
                          "unresolved_identities") == 5
    assert sr._find_value({"a": 1}, "missing") is None


def test_categorize_publish():
    assert sr.categorize_publish(202) == "OK"
    assert sr.categorize_publish(200) == "OK"
    for c in (401, 409, 413, 415, 422):
        assert sr.categorize_publish(c) == "TERMINAL"
    for c in (429, 500, 503):
        assert sr.categorize_publish(c) == "RETRY"
    assert sr.categorize_publish(302) == "TERMINAL"   # неожиданный редирект


def test_bool_parsing():
    assert sr._bool("true") and sr._bool("1") and sr._bool("on")
    assert not sr._bool("false") and not sr._bool(None) and not sr._bool("")


def test_config_reads_env_without_secret_leak():
    env = {"PUBLISH_ENABLED": "false", "SCORING_TZ": "Europe/Moscow",
           "SCHEDULE_TIME": "07:00", "RETRY_TIMES": "07:30,08:00", "WORKERS": "2"}
    cfg = sr.Config(env)
    assert cfg.fire_times == ["07:00", "07:30", "08:00"]
    assert cfg.publish_enabled is False
