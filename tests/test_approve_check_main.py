"""`python approve.py --check` as the "Anything pending?" workflow step runs it:
through __main__, in an environment without any secrets and without config.yaml."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import orders_state
from conftest import SKELETON

ROOT = Path(__file__).resolve().parent.parent
# Copied next to each other so the subprocess imports nothing from the repo root.
NEEDED = ("approve.py", "orders_state.py", "preflight.py", "alerts.py", "notifier.py",
          "config_loader.py")


def _workdir(tmp_path, monkeypatch, pending: bool) -> Path:
    work = tmp_path / "work"
    (work / "config").mkdir(parents=True)
    for name in NEEDED:
        shutil.copy(ROOT / name, work / name)
    orders = work / "config" / "orders.json"
    orders.write_text(json.dumps(SKELETON), encoding="utf-8")
    if pending:
        monkeypatch.setattr(orders_state, "ORDERS_FILE", orders)
        orders_state.put("4711", "pending", plan={"lines": []}, message_id=1)
    assert not (work / "config" / "config.yaml").exists()
    return work


def _secret_free_env(output: Path) -> dict:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR")
           if k in os.environ}
    env["GITHUB_OUTPUT"] = str(output)
    return env


@pytest.mark.parametrize("pending,expected", [(False, "false"), (True, "true")])
def test_check_through_main_exits_0_without_secrets(tmp_path, monkeypatch, pending, expected):
    work = _workdir(tmp_path, monkeypatch, pending)
    output = tmp_path / "github_output"
    proc = subprocess.run([sys.executable, "approve.py", "--check"], cwd=work,
                          env=_secret_free_env(output), capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"has_pending={expected}" in proc.stdout.splitlines()
    assert output.read_text(encoding="utf-8") == f"has_pending={expected}\n"
    assert not (work / "config" / "alerts.json").exists()


def _tracked_record(days_ago, **fields):
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"status": "ordered", "plan": {"booking_nr": "OLRD-0001"}, "message_id": 7,
            "updated_at": when, "verified_at": when,
            "verified_numbers": "90000002 · 01000011/01000012", **fields}


@pytest.mark.parametrize("record,expected", [
    (_tracked_record(1), "true"),                        # tracked: the browser run is needed
    (_tracked_record(22), "false"),                      # older than 21 days
    (_tracked_record(1, klar_closed=True), "false"),     # Klar withdrawn
    (_tracked_record(1, handled=True), "false"),         # Klar tapped (Hanterad)
])
def test_check_through_main_follows_status_tracking(tmp_path, monkeypatch, record, expected):
    work = _workdir(tmp_path, monkeypatch, pending=False)
    (work / "config" / "orders.json").write_text(
        json.dumps({"last_update_id": None, "bookings": {"B1": record}}), encoding="utf-8")
    output = tmp_path / "github_output"
    proc = subprocess.run([sys.executable, "approve.py", "--check"], cwd=work,
                          env=_secret_free_env(output), capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"has_pending={expected}" in proc.stdout.splitlines()
    assert output.read_text(encoding="utf-8") == f"has_pending={expected}\n"
