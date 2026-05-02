"""Admin one-shot: wipe all tenant DB rows. Used to re-test onboarding
from a fresh state after we cut over.

Run on the router via:
    fly ssh console -a rosey-router -C "python /app/router/scripts/wipe_tenants.py"
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text  # noqa: E402

from router import db  # noqa: E402


def main() -> int:
    engine = db.get_engine()
    db.init_db(engine)
    with engine.begin() as conn:
        for tbl in ("members", "onboarding_sessions", "households"):
            n = conn.execute(text(f"DELETE FROM {tbl}")).rowcount
            print(f"  - {tbl}: deleted {n} rows")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
