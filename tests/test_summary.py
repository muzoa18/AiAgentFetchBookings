import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

import notifier
import summary

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 22, 5, 45, tzinfo=timezone.utc)
HIST = [{"timestamp": "2026-10-21T14:29:00.070530", "login_failed": False}]


def rec(status, plate="ABC123", updated="2026-10-21T12:00:00+00:00", **kw):
    return {"status": status, "plan": {"reg_nr": plate}, "message_id": 1,
            "updated_at": updated, **kw}


def orders(**bookings):
    return {"last_update_id": None, "bookings": bookings}


def test_quiet_day():
    text = summary.build_summary(orders(), [], HIST, NOW)
    assert text == "Inget väntar. Senaste lyckade körning: 2026-10-21 UTC"


def test_quiet_day_no_run():
    text = summary.build_summary(orders(), [], [], NOW)
    assert text.startswith("Inget väntar.") and "Ingen körning registrerad" in text


def test_failed_login_run_is_not_success():
    hist = [{"timestamp": "2026-10-21T14:29:00", "login_failed": True}]
    assert "Ingen körning registrerad" in summary.build_summary(orders(), [], hist, NOW)


def test_one_item_in_each_section():
    o = orders(**{
        "P-1": rec("pending", "PEN111"),
        "K-1": rec("ordered", "KLA111", klar_message_id=5, klar_closed=False,
                   verified_at="2026-10-21T13:00:00+00:00"),
        "V-1": rec("ordered", "VER111"),
        "F-1": rec("failed", "FAI111"),
        "O-1": rec("failed", "OLD111", updated="2026-10-01T00:00:00+00:00"),
    })
    alerts = [{"key": "a", "text": "Något larmar", "severity": "error"}]
    text = summary.build_summary(o, alerts, HIST, NOW)
    for head in ("Väntar på ditt godkännande", "Väntar på Klar", "Beställt, ej verifierat",
                 "Kräver åtgärd", "Larm"):
        assert head in text
    sect = {m.group(1): m.group(2) for m in
            re.finditer(r"^(\S[^\n]*?)(?: \(\d+\))?:\n((?:  .*\n?)*)", text, re.M)}
    assert "PEN111" in sect["Väntar på ditt godkännande"]
    assert "KLA111" in sect["Väntar på Klar"]
    assert "VER111" in sect["Beställt, ej verifierat"]
    assert "KLA111" not in sect["Beställt, ej verifierat"]
    assert "FAI111" in sect["Kräver åtgärd"]
    assert "OLD111" not in text            # older than 7 days
    assert "Något larmar" in sect["Larm"]
    assert "Senaste lyckade körning: 2026-10-21 UTC" in text
    assert not text.startswith("Inget väntar")


def test_closed_klar_is_not_waiting():
    o = orders(**{"K-1": rec("ordered", "KLA111", klar_message_id=5, klar_closed=True,
                             verified_at="x")})
    text = summary.build_summary(o, [], HIST, NOW)
    assert "Väntar på Klar" not in text


def test_sixty_pending_collapse_under_cap():
    o = orders(**{f"BOK-{i:03d}": rec("pending", f"PLT{i:03d}") for i in range(60)})
    text = summary.build_summary(o, [], HIST, NOW)
    assert len(text) <= 3000
    assert re.search(r"\+\d+ till", text)


def test_cap_holds_with_many_long_alerts():
    o = orders(**{f"BOK-{i:03d}": rec("pending", f"PLT{i:03d}") for i in range(60)})
    alerts = [{"key": str(i), "text": "x" * 500, "severity": "error"} for i in range(40)]
    assert len(summary.build_summary(o, alerts, HIST, NOW)) <= 3000


def test_alert_text_truncated():
    text = summary.build_summary(orders(), [{"key": "a", "text": "y" * 400}], HIST, NOW)
    assert "y" * 120 in text and "y" * 121 not in text


def test_no_customer_fields():
    r = rec("pending", "PEN111", driver_name="Kalle Kundsson")
    r["plan"]["driver_name"] = "Kalle Kundsson"
    text = summary.build_summary(orders(**{"P-1": r}), [], HIST, NOW)
    assert "Kalle" not in text and "Kundsson" not in text


def test_load_state_missing_files(tmp_path):
    (tmp_path / "config").mkdir()
    o, a, h = summary.load_state(tmp_path)
    assert o["bookings"] == {} and a == [] and h == []


def test_load_state_corrupt_files_give_notes(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "config/orders.json").write_text("{nope", encoding="utf-8")
    (tmp_path / "config/alerts.json").write_text("[1]", encoding="utf-8")
    (tmp_path / "logs/run_history.jsonl").write_bytes(b"\xff\xfe")
    o, a, h = summary.load_state(tmp_path)
    assert o["bookings"] == {} and a == [] and h == []
    text = summary.build_summary(o, a, h, NOW)
    assert "OBS: orders.json kunde inte läsas" in text
    assert "OBS: alerts.json kunde inte läsas" in text
    assert "OBS: run_history.jsonl kunde inte läsas" in text


def test_load_state_merges_unresolved_alerts_and_last_five_history(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "logs").mkdir()
    al = {"alerts": {"k1": {"text": "ett", "severity": "error", "last_seen": "2026-10-21T10:00:00+00:00"},
                     "k2": {"text": "klart", "resolved_at": "2026-10-21T11:00:00+00:00"}}}
    wd = {"alerts": {"stale_bookings": {"text": "två", "severity": "error",
                                        "last_seen": "2026-10-21T12:00:00+00:00"}}}
    (tmp_path / "config/alerts.json").write_text(json.dumps(al), encoding="utf-8")
    (tmp_path / "config/watchdog_alerts.json").write_text(json.dumps(wd), encoding="utf-8")
    (tmp_path / "logs/run_history.jsonl").write_text(
        "\n".join(json.dumps({"timestamp": f"2026-10-0{i}T00:00:00"}) for i in range(1, 9)) + "\n",
        encoding="utf-8")
    o, a, h = summary.load_state(tmp_path)
    assert {x["text"] for x in a} == {"ett", "två"}
    assert [x["timestamp"][:10] for x in h] == [f"2026-10-0{i}" for i in range(4, 9)]
    text = summary.build_summary(o, a, h, NOW)
    assert "OBS" not in text.replace("OBS: orders.json", "") or "kunde inte" not in text


def _env():
    return {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}


def test_main_sends_exactly_one_message(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    sent = []
    monkeypatch.setattr(notifier, "send_message", lambda c, t: sent.append((c, t)) or True)
    assert summary.main(_env(), NOW, tmp_path) == 0
    assert len(sent) == 1
    assert sent[0][0]["telegram"] == {"bot_token": "t", "chat_id": "1"}
    assert "Inget väntar" in sent[0][1]


def test_main_returns_zero_when_sending_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, "send_message", lambda c, t: False)
    assert summary.main(_env(), NOW, tmp_path) == 0


def test_main_returns_zero_when_sending_raises(tmp_path, monkeypatch):
    def boom(c, t):
        raise RuntimeError("x")
    monkeypatch.setattr(notifier, "send_message", boom)
    assert summary.main(_env(), NOW, tmp_path) == 0


def test_summary_is_stdlib_only():
    src = (ROOT / "summary.py").read_text(encoding="utf-8")
    for mod in re.findall(r"^(?:import|from)\s+([A-Za-z_]\w*)", src, re.M):
        assert mod in sys.stdlib_module_names or mod == "notifier", mod


def test_summary_workflow_structure():
    wf = yaml.safe_load((ROOT / ".github/workflows/daily-summary.yml").read_text(encoding="utf-8"))
    assert wf["name"] == "MECA Daily Summary"
    on = wf.get("on", wf.get(True))
    assert on["schedule"] == [{"cron": "45 5 * * *"}] and "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"] == {"group": "meca-summary", "cancel-in-progress": False}
    (job,) = wf["jobs"].values()
    assert job["timeout-minutes"] == 5
    steps = job["steps"]
    run = next(s for s in steps if s.get("run", "").strip() == "python3 summary.py")
    assert set(run["env"]) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    dumped = yaml.safe_dump(wf)
    assert "pip install" not in dumped and "git push" not in dumped
    assert "git commit" not in dumped and "alerts" not in dumped
    assert "cache" not in yaml.safe_dump(steps[1])
    assert steps[-1] is run
