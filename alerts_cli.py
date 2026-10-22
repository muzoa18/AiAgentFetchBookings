"""
alerts_cli.py
-------------
Workflow-level alerts (stdlib only: runs on a runner where nothing is
pip-installed, so it also covers a failed dependency install, a step timeout,
a job-timeout cancel or a crash before our own code starts).

    python alerts_cli.py workflow-failed <workflow-name>
    python alerts_cli.py workflow-ok <workflow-name>

workflow-failed raises workflow_failed_<workflow-name>, unless this run already
raised a more specific alert for the workflow (state_unreadable, crash_<short>
or run_failed_<short>, still active and last seen within FRESH): that alert has
already told the owner, so one fault gives one message.

workflow-ok (the "Mark OK" step, on success) resolves workflow_failed_<name>,
run_failed_<short> and crash_<short>. It needs no secrets. Most approve runs
stop after `approve.py --check`, so without it their alerts would stay red.

<short> is the guarded() name (preflight.DISPLAY_NAMES inverted); an unknown
workflow name has none, so only workflow_failed_<name> is touched.

Env (workflow-failed): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GITHUB_SERVER_URL,
GITHUB_REPOSITORY, GITHUB_RUN_ID. Always exits 0: an alert helper must not mask
or add failures.
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import alerts
from preflight import DISPLAY_NAMES, STATE_ALERT_KEY

log = logging.getLogger("alerts_cli")

SHORT_NAMES = {display: short for short, display in DISPLAY_NAMES.items()}
FRESH = timedelta(minutes=20)


def _specific_keys(name: str) -> list[str]:
    short = SHORT_NAMES.get(name)
    if short is None:
        return []
    return [STATE_ALERT_KEY, f"crash_{short}", f"run_failed_{short}"]


def _fresh_specific_alert(name: str, now: datetime) -> str | None:
    """Key of an active specific alert of this run, or None. Never raises: when in
    doubt the generic alert is sent."""
    try:
        keys = set(_specific_keys(name))
        for entry in alerts.active():
            if entry["key"] not in keys:
                continue
            seen = alerts.parse_time(entry.get("last_seen"))
            if seen is not None and abs(now - seen) <= FRESH:
                return entry["key"]
    except Exception as e:
        log.error("could not check for a specific alert (%s).", type(e).__name__)
    return None


def workflow_failed(name: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    specific = _fresh_specific_alert(name, now)
    if specific:
        log.info("workflow_failed_%s suppressed: %s was raised by this run.", name, specific)
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN missing: cannot send the failure alert.")
        return
    url = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GITHUB_RUN_ID", ""))
    config = {"telegram": {"bot_token": token,
                           "chat_id": os.environ.get("TELEGRAM_CHAT_ID", "")}}
    alerts.raise_alert(config, f"workflow_failed_{name}",
                       f"GitHub-körningen {name} misslyckades: {url}")


def workflow_ok(name: str) -> None:
    keys = [f"workflow_failed_{name}"]
    short = SHORT_NAMES.get(name)
    if short is not None:
        keys += [f"run_failed_{short}", f"crash_{short}"]
    for key in keys:
        if alerts.resolve(key):
            log.info("Resolved %s.", key)


COMMANDS = {"workflow-failed": workflow_failed, "workflow-ok": workflow_ok}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 2 and argv[0] in COMMANDS:
            COMMANDS[argv[0]](argv[1])
        else:
            log.error("usage: alerts_cli.py workflow-failed|workflow-ok <workflow-name>")
    except Exception as e:
        log.error("alerts_cli failed (%s).", type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
