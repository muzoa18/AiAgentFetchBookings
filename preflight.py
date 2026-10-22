"""
preflight.py
------------
Fail-closed guards for the entry points (agent.py, approve.py).

- state_problems / check_state_or_alert: a missing or unreadable config/orders.json
  stops all ordering and raises an alert (orders_state._load() alone treats a missing
  file as empty, which once re-staged an order that was already placed).
- guarded: wraps a run so a crash or a non-zero exit raises an alert.

Alert texts are fixed Swedish strings; they never contain exception messages
(only the exception class name), since messages can carry page or customer text.
"""

import logging
from typing import Callable

import alerts
import orders_state
from config_loader import load_config

log = logging.getLogger(__name__)

STATE_ALERT_KEY = "state_unreadable"
MISSING = "config/orders.json saknas"
UNREADABLE = "config/orders.json kan inte läsas"


def state_problems() -> list[str]:
    if not orders_state.ORDERS_FILE.exists():
        return [MISSING]
    try:
        orders_state._load()
    except Exception:
        return [UNREADABLE]
    return []


def check_state_or_alert(config) -> bool:
    problems = state_problems()
    if problems:
        log.error("Order state unusable (%s) — stopping before any ordering.", "; ".join(problems))
        alerts.raise_alert(config, STATE_ALERT_KEY, "; ".join(problems))
        return False
    alerts.resolve(STATE_ALERT_KEY)
    return True


def guarded(name: str, fn: Callable[[], int], config_loader=load_config) -> int:
    try:
        code = fn()
    except Exception as e:
        log.exception("%s crashed.", name)
        config = _config(config_loader)
        if config is not None:
            alerts.raise_alert(config, f"crash_{name}",
                               f"Körningen av {name} kraschade ({type(e).__name__}) – "
                               "se GitHub Actions-loggen.")
        return 1
    config = _config(config_loader)
    if config is None:
        return code
    if code == 0:
        alerts.resolve(f"crash_{name}")
        alerts.resolve(f"run_failed_{name}")
    else:
        alerts.raise_alert(config, f"run_failed_{name}",
                           f"Körningen av {name} slutade med fel – se GitHub Actions-loggen.")
    return code


def _config(config_loader):
    try:
        return config_loader()
    except Exception:
        log.exception("Config could not be loaded — no alert can be sent.")
        return None
