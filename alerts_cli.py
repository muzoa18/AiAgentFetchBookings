"""
alerts_cli.py
-------------
Workflow-level failure alert (stdlib only: runs on a runner where nothing is
pip-installed, so it also covers a failed dependency install, a timeout or a
crash before our own code starts).

    python alerts_cli.py workflow-failed <workflow-name>

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GITHUB_SERVER_URL, GITHUB_REPOSITORY,
GITHUB_RUN_ID. Always exits 0: an alert helper must not mask or add failures.
"""

import logging
import os
import sys

import alerts

log = logging.getLogger("alerts_cli")


def workflow_failed(name: str) -> None:
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


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 2 and argv[0] == "workflow-failed":
            workflow_failed(argv[1])
        else:
            log.error("usage: alerts_cli.py workflow-failed <workflow-name>")
    except Exception as e:
        log.error("alerts_cli failed (%s).", type(e).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
