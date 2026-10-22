"""
watchdog.py
-----------
Dead-man switch (stdlib only: runs without pip install). Asks the GitHub API
when bookings.yml and approve.yml last succeeded and raises a Telegram alert if
a scheduled slot was missed. A failing API call is NOT evidence of a stale run:
it is logged and nothing is raised.

Env: GITHUB_TOKEN, GITHUB_REPOSITORY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Always exits 0.
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import alerts

log = logging.getLogger("watchdog")


class WatchdogError(Exception):
    pass


@dataclass(frozen=True)
class Spec:
    file: str
    days: tuple          # datetime.weekday() values with a scheduled run
    hour: int            # UTC
    key: str
    label: str
    display_name: str    # the workflow's `name:`, used in workflow_failed_<name>


SPECS = {
    "bookings": Spec(file="bookings.yml", days=(0, 1, 2, 3, 4, 5), hour=15,
                     key="stale_bookings", label="bokningskontrollen",
                     display_name="MECA Booking Agent"),
    "approve": Spec(file="approve.yml", days=(0, 1, 2, 3, 4, 5, 6), hour=6,
                    key="stale_approve", label="godkännandekontrollen",
                    display_name="MECA Approval Poller"),
}


def _utc(when: datetime) -> datetime:
    """`when` in UTC; a naive datetime is taken as UTC (never as the machine's local time)."""
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def last_slot(now: datetime, spec: Spec) -> datetime:
    """Latest scheduled slot <= now (UTC; a naive `now` is UTC)."""
    now = _utc(now)
    for back in range(8):
        day = now - timedelta(days=back)
        if day.weekday() not in spec.days:
            continue
        slot = day.replace(hour=spec.hour, minute=0, second=0, microsecond=0)
        if slot <= now:
            return slot
    raise WatchdogError("no slot found")


def is_overdue(now, last_success, spec, grace=timedelta(hours=3)) -> bool:
    now = _utc(now)
    if last_success is not None:
        last_success = _utc(last_success)
    slot = last_slot(now, spec)
    if now <= slot + grace:
        return False
    # A cron run may start late; one that started up to 30 min before the slot is no miss.
    return last_success is None or last_success < slot - timedelta(minutes=30)


def fetch_last_success(repo: str, token: str, workflow_file: str,
                       urlopen=urllib.request.urlopen):
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow_file}/runs?status=success&per_page=1")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data["workflow_runs"]
        if not items:
            return None
        created = datetime.fromisoformat(items[0]["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created
    except Exception as e:
        # Type only: the message could echo the URL or headers.
        raise WatchdogError(f"GitHub API lookup failed ({type(e).__name__})") from None


def _resolve_old_failure(spec: Spec, last_success: datetime) -> None:
    key = f"workflow_failed_{spec.display_name}"
    for a in alerts.active():
        if a["key"] != key:
            continue
        seen = alerts._parse(a.get("last_seen"))
        if seen is not None and seen < last_success:
            alerts.resolve(key)


def main(env=None, now=None, urlopen=urllib.request.urlopen) -> int:
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    config = {"telegram": {"bot_token": env.get("TELEGRAM_BOT_TOKEN", ""),
                           "chat_id": env.get("TELEGRAM_CHAT_ID", "")}}
    for spec in SPECS.values():
        try:
            last = fetch_last_success(env.get("GITHUB_REPOSITORY", ""),
                                      env.get("GITHUB_TOKEN", ""), spec.file, urlopen)
            if is_overdue(now, last, spec):
                since = last.strftime("%Y-%m-%d") if last else "okänt datum"
                alerts.raise_alert(
                    config, spec.key,
                    f"Ingen lyckad körning av {spec.label} sedan {since} UTC. "
                    "Kontrollera GitHub Actions.")
            else:
                alerts.resolve(spec.key)
                if last is not None:
                    _resolve_old_failure(spec, last)
        except WatchdogError as e:
            log.error("%s: %s (no alert raised).", spec.file, e)
        except Exception as e:
            log.error("%s: watchdog failed (%s).", spec.file, type(e).__name__)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
