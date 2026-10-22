"""
preflight.py
------------
Fail-closed guards for the entry points (agent.py, approve.py).

- state_problems / check_state_or_alert: a missing or unreadable config/orders.json
  stops all ordering and raises an alert (orders_state._load() alone treats a missing
  file as empty, which once re-staged an order that was already placed).
- guarded: wraps a run so a crash or a non-zero exit raises an alert. A successful run
  resolves crash_<name>, run_failed_<name> and the workflow_failed_<workflow name> alert
  of alerts_cli without loading the config (alerts.json has one writer group,
  bookings/approve; the watchdog keeps its own file).
- One fault, one message: when this run already raised a more specific alert
  (state_unreadable from check_state_or_alert, or crash_<name>), a non-zero exit does not
  add run_failed_<name> on top. alerts_cli workflow-failed applies the same rule.

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
# guarded() name -> the workflow name alerts_cli gets ($WORKFLOW_NAME = github.workflow).
DISPLAY_NAMES = {"booking_agent": "MECA Booking Agent", "approve": "MECA Approval Poller"}

# Set when this run raised a more specific alert than run_failed_<name>; reset by guarded().
_specific_alert_raised = False


def state_problems() -> list[str]:
    if not orders_state.ORDERS_FILE.exists():
        return [MISSING]
    try:
        orders_state._load()
    except Exception:
        return [UNREADABLE]
    return []


def check_state_or_alert(config) -> bool:
    global _specific_alert_raised
    problems = state_problems()
    if problems:
        _specific_alert_raised = True
        log.error("Order state unusable (%s) — stopping before any ordering.", "; ".join(problems))
        alerts.raise_alert(config, STATE_ALERT_KEY, "; ".join(problems))
        return False
    alerts.resolve(STATE_ALERT_KEY)
    return True


def resolve_success(name: str) -> None:
    """The alerts a successful run of <name> clears. Needs no config."""
    alerts.resolve(f"crash_{name}")
    alerts.resolve(f"run_failed_{name}")
    if name in DISPLAY_NAMES:
        alerts.resolve(f"workflow_failed_{DISPLAY_NAMES[name]}")


def guarded(name: str, fn: Callable[[], int], config_loader=load_config) -> int:
    global _specific_alert_raised
    _specific_alert_raised = False
    try:
        code = fn()
    except Exception as e:
        log.exception("%s crashed.", name)
        _specific_alert_raised = True
        config = _config(config_loader)
        if config is not None:
            alerts.raise_alert(config, f"crash_{name}",
                               f"Körningen av {name} kraschade ({type(e).__name__}) – "
                               "se GitHub Actions-loggen.")
        return 1
    if code == 0:
        resolve_success(name)
        return code
    if _specific_alert_raised:
        log.info("run_failed_%s not raised: this run already raised a more specific alert.",
                 name)
        return code
    config = _config(config_loader)
    if config is not None:
        alerts.raise_alert(config, f"run_failed_{name}",
                           f"Körningen av {name} slutade med fel – se GitHub Actions-loggen.")
    return code


def _config(config_loader):
    # load_config() calls sys.exit(1) when secrets are missing: contain that too
    # (SystemExit is not an Exception). KeyboardInterrupt still propagates.
    try:
        return config_loader()
    except (Exception, SystemExit):
        log.exception("Config could not be loaded — no alert can be sent.")
        return None
