"""Saturday-morning household digest: ask the agent to produce a summary,
text it to every household member.

Run once: `python -m summary`
Run on schedule: `python -m summary --schedule`  (Sat 9am local)
"""

from __future__ import annotations

import argparse
import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

import channels
import roster
from agent import handle_message

log = logging.getLogger("rosey.summary")

DIGEST_TASK = (
    "Produce the Saturday morning household digest. "
    "Read whatever is in /memories that's relevant: pending groceries, "
    "open reminders, the upcoming week from events.md, anything else "
    "worth surfacing. Group by domain (Week ahead / Groceries / "
    "Reminders / Other), one short line per item. Under 'Week ahead', "
    "list the next 7 days of events with date + short description "
    "(skip @-mentions). If nothing is pending in a section, omit it. "
    "End with a one-line note about the week if anything stands out — "
    "otherwise omit. Plain text, under 800 characters, suitable for "
    "Telegram."
)


def format_summary() -> str:
    """Invoke the agent as a system task. Returns the digest text."""
    return handle_message("+system", DIGEST_TASK, is_system=True)


def send_to_household(text: str) -> None:
    members = roster.members()
    if not members:
        log.warning("no household members found in household.md — nothing to send")
        print(text)
        return

    for m in members:
        if channels.send(m.identifier, text):
            log.info("sent summary to %s (%s)", m.name, m.identifier)
        else:
            log.warning("failed to send summary to %s (%s)", m.name, m.identifier)


def run_once() -> None:
    log.info("building summary")
    text = format_summary()
    log.info("summary ready (%d chars)", len(text))
    if text:
        send_to_household(text)
    else:
        log.warning("agent returned empty summary — nothing sent")


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--schedule", action="store_true", help="run on cron (Sat 9am local) instead of once"
    )
    args = parser.parse_args()

    if not args.schedule:
        run_once()
        return 0

    scheduler = BlockingScheduler()
    scheduler.add_job(run_once, CronTrigger(day_of_week="sat", hour=9, minute=0))
    log.info("scheduler running — Saturday 9:00 local time")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
