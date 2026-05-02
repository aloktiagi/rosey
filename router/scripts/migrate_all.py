"""Admin: redeploy every household VM from the latest rosey-template image.

When you ship a new feature to the household codebase, the steps are:

  1. fly deploy -a rosey-template            # new image in the registry
  2. python -m router.scripts.migrate_all    # this script

This loops the tenant DB and runs `fly deploy --image
registry.fly.io/rosey-template:<latest> -a <fly_app_name>` for each
household. Sequential with a brief gap so we don't stampede Fly's
control plane.

Volumes survive deploys, so household memory (groceries, threads,
reminders) is preserved.

Run from the router VM:
    fly ssh console -a rosey-router \\
      -C "python /app/router/scripts/migrate_all.py"
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text  # noqa: E402

from router import db  # noqa: E402

log = logging.getLogger("rosey.migrate")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

DELAY_BETWEEN_DEPLOYS_SEC = 5
DEPLOY_TIMEOUT_SEC = 600


def _latest_image_of(app: str) -> str:
    out = subprocess.run(
        ["fly", "image", "show", "-a", app, "--json"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"fly image show -a {app} failed: {out.stderr.strip()}")
    data = json.loads(out.stdout)
    if isinstance(data, list):
        data = data[0]
    return f"{data['Registry']}/{data['Repository']}:{data['Tag']}"


def _redeploy(app_name: str, image: str, config_path: str) -> bool:
    cmd = [
        "fly", "deploy",
        "--image", image,
        "-a", app_name,
        "--config", config_path,
        "--yes",
    ]
    log.info("$ %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=DEPLOY_TIMEOUT_SEC)
    if result.returncode != 0:
        log.error("redeploy failed for %s\nstderr: %s", app_name, result.stderr.strip()[-400:])
        return False
    log.info("redeploy succeeded for %s", app_name)
    return True


def main() -> int:
    template_app = os.environ.get("ROSEY_TEMPLATE_APP", "rosey-template")
    config_path = os.environ.get(
        "ROSEY_HOUSEHOLD_CONFIG",
        str(Path(__file__).resolve().parent.parent / "household_template.fly.toml"),
    )

    image = _latest_image_of(template_app)
    log.info("migrating all households to %s", image)

    engine = db.get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, fly_app_name FROM households WHERE status = 'active' ORDER BY created_at")
        ).mappings().all()

    log.info("found %d active household(s)", len(rows))

    succeeded, failed = [], []
    for r in rows:
        ok = _redeploy(r["fly_app_name"], image, config_path)
        (succeeded if ok else failed).append(r["fly_app_name"])
        time.sleep(DELAY_BETWEEN_DEPLOYS_SEC)

    log.info("done. succeeded=%d failed=%d", len(succeeded), len(failed))
    if failed:
        log.warning("retry these: %s", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
