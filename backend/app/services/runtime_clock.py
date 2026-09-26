"""Resolve the clock used by request-time planning operations.

Demo and test runs retain the fixed anchor for reproducibility. Local use follows
the machine clock so a newly imported order is not scheduled in the demo month.
"""

from __future__ import annotations

from datetime import datetime

from app.seed.dataset import DEMO_ANCHOR


def operational_now(app_env: str) -> datetime:
    return DEMO_ANCHOR if app_env in {"DEMO", "TEST"} else datetime.now()
