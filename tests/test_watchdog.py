import io
import json
import re
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import alerts
import notifier
import watchdog
from watchdog import SPECS, is_overdue, last_slot

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123:SECRET-TELEGRAM"
GH_TOKEN = "ghs_SECRETGITHUB"
B, A = SPECS["bookings"], SPECS["approve"]


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# 2026-10-21 is a Wednesday
def test_last_slot_cases():
    assert last_slot(utc(2026, 10, 21, 14), B) == utc(2026, 10, 20, 15)
    assert last_slot(utc(2026, 10, 21, 15, 30), B) == utc(2026, 10, 21, 15)
    assert last_slot(utc(2026, 10, 25, 20), B) == utc(2026, 10, 24, 15)   # Sunday
    assert last_slot(utc(2026, 10, 26, 10), B) == utc(2026, 10, 24, 15)   # Monday
    assert last_slot(utc(2026, 10, 25, 7), A) == utc(2026, 10, 25, 6)


def test_specs_values():
    assert B.file == "bookings.yml" and B.days == (0, 1, 2, 3, 4, 5) and B.hour == 15
    assert B.key == "stale_bookings" and B.label == "bokningskontrollen"
    assert B.display_name == "MECA Booking Agent"
    assert A.file == "approve.yml" and A.days == tuple(range(7)) and A.hour == 6
    assert A.key == "stale_approve" and A.label == "godkännandekontrollen"
    assert A.display_name == "MECA Approval Poller"


def test_is_overdue_cases():
    tue = utc(2026, 10, 20, 15, 2)
    assert not is_overdue(utc(2026, 10, 21, 14), tue, B)
    assert is_overdue(utc(2026, 10, 21, 18, 30), tue, B)
    assert not is_overdue(utc(2026, 10, 21, 15, 20), tue, B)
    assert not is_overdue(utc(2026, 10, 25, 20), utc(2026, 10, 24, 15, 5), B)
    now = utc(2026, 10, 21, 18, 30)
    assert not is_overdue(now, utc(2026, 10, 21, 14, 40), B)   # 20 min before slot
    assert is_overdue(now, utc(2026, 10, 21, 14, 20), B)       # 40 min before slot
    assert is_overdue(now, None, B)
    assert not is_overdue(utc(2026, 10, 21, 15, 20), None, B)  # inside grace


def test_naive_now_is_treated_as_utc_never_local_time():
    naive = datetime(2026, 10, 21, 15, 30)               # meant as 15:30 UTC
    assert last_slot(naive, B) == utc(2026, 10, 21, 15)
    assert last_slot(datetime(2026, 10, 21, 14, 59), B) == utc(2026, 10, 20, 15)
    tue = utc(2026, 10, 20, 15, 2)
    assert is_overdue(datetime(2026, 10, 21, 18, 30), tue, B)
    assert not is_overdue(datetime(2026, 10, 21, 17, 59), tue, B)   # inside grace
    # A naive last_success is UTC too (no TypeError comparing naive and aware).
    assert not is_overdue(utc(2026, 10, 21, 18, 30), datetime(2026, 10, 21, 15, 1), B)


class Resp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(by_file, seen=None):
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req)
        for f, v in by_file.items():
            if f"/workflows/{f}/" in req.full_url:
                if isinstance(v, Exception):
                    raise v
                return Resp(v)
        raise AssertionError(req.full_url)
    return _open


def runs(ts):
    return {"workflow_runs": [{"created_at": ts}] if ts else []}


def test_fetch_last_success_parses_and_authenticates():
    seen = []
    got = watchdog.fetch_last_success(
        "o/r", GH_TOKEN, "bookings.yml",
        fake_urlopen({"bookings.yml": runs("2026-10-20T15:02:11Z")}, seen))
    assert got == utc(2026, 10, 20, 15, 2, 11)
    assert "o/r/actions/workflows/bookings.yml/runs" in seen[0].full_url
    assert "status=success" in seen[0].full_url
    assert GH_TOKEN in seen[0].get_header("Authorization")


def test_fetch_last_success_none_when_no_runs():
    assert watchdog.fetch_last_success(
        "o/r", GH_TOKEN, "x.yml", fake_urlopen({"x.yml": runs(None)})) is None


@pytest.mark.parametrize("bad", [urllib.error.URLError("boom"), b"not json",
                                 {"workflow_runs": [{"created_at": "nope"}]},
                                 {"other": 1}])
def test_fetch_last_success_raises_watchdog_error(bad):
    with pytest.raises(watchdog.WatchdogError):
        watchdog.fetch_last_success("o/r", GH_TOKEN, "x.yml", fake_urlopen({"x.yml": bad}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    # The watchdog writes only its own file; the tests read it back through alerts.
    monkeypatch.setattr(alerts, "ALERTS_FILE", tmp_path / "alerts.json")
    monkeypatch.setattr(watchdog, "WATCHDOG_ALERTS_FILE", tmp_path / "alerts.json")
    return {"GITHUB_TOKEN": GH_TOKEN, "GITHUB_REPOSITORY": "o/r",
            "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notifier, "send_message",
                        lambda config, text, *a, **k: out.append(text) or True)
    return out


NOW = utc(2026, 10, 21, 18, 30)
FRESH = {"bookings.yml": runs("2026-10-21T15:02:00Z"),
         "approve.yml": runs("2026-10-21T06:01:00Z")}


def test_main_stale_alerts_once(env, sent):
    data = dict(FRESH, **{"bookings.yml": runs("2026-10-20T15:02:00Z")})
    assert watchdog.main(env, NOW, fake_urlopen(data)) == 0
    assert len(sent) == 1
    assert "Ingen lyckad körning av bokningskontrollen sedan 2026-10-20" in sent[0]
    assert "Kontrollera GitHub Actions." in sent[0]
    assert [a["key"] for a in alerts.active()] == ["stale_bookings"]
    assert watchdog.main(env, NOW, fake_urlopen(data)) == 0
    assert len(sent) == 1  # de-duplicated


def test_main_stale_with_no_success_at_all(env, sent):
    data = dict(FRESH, **{"approve.yml": runs(None)})
    assert watchdog.main(env, NOW, fake_urlopen(data)) == 0
    assert len(sent) == 1 and "godkännandekontrollen" in sent[0]


def test_main_fresh_resolves_stale_alert(env, sent):
    alerts.raise_alert({}, "stale_bookings", "x", now=NOW - timedelta(days=1))
    assert watchdog.main(env, NOW, fake_urlopen(FRESH)) == 0
    assert alerts.active() == []


def test_main_writes_only_the_watchdog_file_and_restores_the_alerts_path(
        tmp_path, monkeypatch, sent):
    shared = tmp_path / "alerts.json"              # bookings/approve's file: never written
    own = tmp_path / "watchdog_alerts.json"
    monkeypatch.setattr(alerts, "ALERTS_FILE", shared)
    monkeypatch.setattr(watchdog, "WATCHDOG_ALERTS_FILE", own)
    env = {"GITHUB_TOKEN": GH_TOKEN, "GITHUB_REPOSITORY": "o/r",
           "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}
    data = dict(FRESH, **{"bookings.yml": runs("2026-10-20T15:02:00Z")})

    assert watchdog.main(env, NOW, fake_urlopen(data)) == 0

    assert not shared.exists()
    assert set(json.loads(own.read_text(encoding="utf-8"))["alerts"]) == {"stale_bookings"}
    assert alerts.ALERTS_FILE == shared             # global restored after main


def test_default_watchdog_file():
    assert watchdog.WATCHDOG_ALERTS_FILE == Path("config/watchdog_alerts.json")


def test_main_no_longer_touches_workflow_failed_alerts(env, sent):
    alerts.raise_alert({}, "workflow_failed_MECA Booking Agent", "x",
                       now=utc(2026, 10, 20, 15, 5))
    watchdog.main(env, NOW, fake_urlopen(FRESH))
    assert [a["key"] for a in alerts.active()] == ["workflow_failed_MECA Booking Agent"]
    assert not hasattr(watchdog, "_resolve_old_failure")


@pytest.mark.parametrize("bad", [urllib.error.URLError("boom"), b"garbage"])
def test_main_api_error_no_alert(env, sent, bad, capsys, caplog):
    assert watchdog.main(env, NOW, fake_urlopen({"bookings.yml": bad, "approve.yml": bad})) == 0
    assert sent == [] and alerts.active() == []
    out = capsys.readouterr()
    assert GH_TOKEN not in out.out + out.err + caplog.text


def test_main_never_logs_tokens(env, sent, capsys, caplog):
    data = dict(FRESH, **{"bookings.yml": runs("2026-10-20T15:02:00Z")})
    watchdog.main(env, NOW, fake_urlopen(data))
    out = capsys.readouterr()
    blob = out.out + out.err + caplog.text
    assert TOKEN not in blob and GH_TOKEN not in blob


def test_watchdog_is_stdlib_only():
    src = (ROOT / "watchdog.py").read_text(encoding="utf-8")
    for mod in re.findall(r"^(?:import|from)\s+([A-Za-z_]\w*)", src, re.M):
        assert mod in sys.stdlib_module_names or mod in ("alerts", "notifier"), mod


def test_watchdog_workflow_structure():
    wf = yaml.safe_load((ROOT / ".github/workflows/watchdog.yml").read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))
    assert on["schedule"] == [{"cron": "17 */6 * * *"}] and "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "write", "actions": "read"}
    # Its own group: in the shared one a queued watchdog run would replace a pending
    # bookings/approve run (GitHub keeps only one pending run per group).
    assert wf["concurrency"] == {"group": "meca-watchdog", "cancel-in-progress": False}
    job = wf["jobs"]["watch"]
    assert job["timeout-minutes"] == 5
    steps = job["steps"]
    run = next(s for s in steps if s.get("run", "").strip() == "python watchdog.py")
    assert set(run["env"]) == {"GITHUB_TOKEN", "GITHUB_REPOSITORY",
                               "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    persist = steps[-1]
    # Its own state file: bookings/approve own config/alerts.json (no shared writer).
    assert "git add config/watchdog_alerts.json" in persist["run"]
    assert "config/alerts.json" not in persist["run"]
    assert "chore: update watchdog state [skip ci]" in persist["run"]
    assert "pip install" not in yaml.safe_dump(wf)
    assert "cache" not in yaml.safe_dump(steps[1])
