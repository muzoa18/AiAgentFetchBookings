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
    assert text.count("BOK-") == 10 and "+50 till" in text


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
    assert "kunde inte läsas" not in text.replace("OBS: orders.json kunde inte läsas", "")
    assert "ett" in text and "två" in text and "klart" not in text


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


def _sect(text, title):
    m = re.search(rf"^{re.escape(title)} \(\d+\):\n((?:  .*\n?)*)", text, re.M)
    return m.group(1) if m else ""


def test_klar_section_each_booking_once_and_annotated():
    o = orders(**{
        "U-1": rec("ordered", "UNV111", klar_message_id=5),
        "B-1": rec("blocked", "BLO111", klar_message_id=6),
        "V-1": rec("ordered", "VER111", klar_message_id=7, verified_at="x"),
    })
    text = summary.build_summary(o, [], HIST, NOW)
    for plate in ("UNV111", "BLO111", "VER111"):
        assert text.count(plate) == 1
    k = _sect(text, "Väntar på Klar")
    assert "UNV111 (ej verifierad)" in k and "BLO111 (blockerad)" in k and "VER111" in k
    assert "Beställt, ej verifierat" not in text and "Kräver åtgärd" not in text


def test_klar_not_outstanding_cases_fall_through_or_vanish():
    old = "2026-10-01T00:00:00+00:00"   # 21 days before NOW
    o = orders(**{
        "H-1": rec("ordered", "HAN111", klar_message_id=5, handled=True, verified_at="x"),
        "O-1": rec("ordered", "OLD111", klar_message_id=5, klar_sent_at=old),
        "T-1": rec("ordered", "BOO111", klar_message_id=True, verified_at="x"),
        "S-1": rec("ordered", "STR111", klar_message_id="5", verified_at="x"),
        "C-1": rec("cancelled", "CAN111", klar_message_id=5),
    })
    text = summary.build_summary(o, [], HIST, NOW)
    assert "Väntar på Klar" not in text
    assert "HAN111" not in text and "BOO111" not in text and "STR111" not in text
    assert "CAN111" not in text
    assert "OLD111" in _sect(text, "Beställt, ej verifierat")   # expired Klar: plain unverified


def test_klar_predicate_matches_orders_state(tmp_path, monkeypatch):
    import orders_state
    recs = {
        "a": rec("ordered", klar_message_id=1),
        "b": rec("ordered", klar_message_id=1, klar_sent_at="2026-10-20T00:00:00+00:00"),
        "c": rec("ordered", klar_message_id=1, klar_sent_at="2026-10-01T00:00:00+00:00"),
        "d": rec("ordered", klar_message_id=1, klar_sent_at="2026-10-08T05:45:00+00:00"),
        "e": rec("blocked", klar_message_id=1, handled=True),
        "f": rec("blocked", klar_message_id=1, klar_closed=True),
        "i": rec("ordered"),
        "j": rec("ordered", klar_message_id=1, handled=False, klar_closed=False),
    }
    path = tmp_path / "orders.json"
    path.write_text(json.dumps({"last_update_id": None, "bookings": recs}), encoding="utf-8")
    monkeypatch.setattr(orders_state, "ORDERS_FILE", path)
    # (bool/str klar_message_id cannot be loaded by orders_state at all: covered above.)
    expected = {bid for bid, _ in orders_state.outstanding_klar(NOW)}
    assert expected  # the fixture exercises both outcomes
    assert {bid for bid, r in recs.items() if summary.klar_outstanding(r, NOW)} == expected
    assert summary.KLAR_MAX_AGE == orders_state.KLAR_MAX_AGE


def test_alert_text_none_is_skipped():
    alerts = [{"key": "a", "text": None}, {"key": "b", "text": ""}]
    assert summary.build_summary(orders(), alerts, HIST, NOW).startswith("Inget väntar")
    text = summary.build_summary(orders(), alerts + [{"key": "c", "text": "ok"}], HIST, NOW)
    assert "None" not in text and "ok" in _sect(text, "Larm")


def test_action_record_without_usable_updated_at_is_shown():
    o = orders(**{"F-1": {"status": "failed", "plan": {"reg_nr": "FAI111"}},
                  "F-2": rec("blocked", "BLO111", updated="garbage")})
    k = _sect(summary.build_summary(o, [], HIST, NOW), "Kräver åtgärd")
    assert "FAI111" in k and "BLO111" in k


def test_collapse_loop_and_hard_slice(monkeypatch):
    o = orders(**{f"BOK-{i:03d}": rec("pending", f"PLT{i:03d}") for i in range(8)})
    monkeypatch.setattr(summary, "SECTION_MAX", 8)
    monkeypatch.setattr(summary, "MAX_CHARS", 200)
    text = summary.build_summary(o, [], HIST, NOW)
    assert len(text) <= 200 and "+" in text and "till" in text
    monkeypatch.setattr(summary, "MAX_CHARS", 30)     # even the header+footer overflow
    text = summary.build_summary(o, [], HIST, NOW)
    assert len(text) == 30
