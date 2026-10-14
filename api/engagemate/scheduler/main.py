"""Scheduler entrypoint: enqueue due account jobs on a fixed interval.

Run with `python -m engagemate.scheduler.main`. The process owns no tenant
state — every tick re-queries the database and relies on `schedule_key`
uniqueness so restarts and overlapping ticks never double-book a window.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable
from datetime import datetime, timezone

from engagemate.scheduler.service import Scheduler

logger = logging.getLogger(__name__)

# Above Neon's 300s idle-suspend threshold, for the same reason the worker's
# idle poll is: a shorter interval keeps a scale-to-zero compute permanently
# awake and bills ~730 h/month with no users connected. Raising only the worker
# achieved nothing while this sat at 60s, because the endpoint suspends on
# idleness and the FASTEST poller decides whether idleness is ever reached.
#
# The cost is enqueue latency. `enqueue_due` inserts one job per account per
# local day once `schedule_hour` arrives, so a window is picked up within ten
# minutes rather than one. Do not raise this to hours: an 8h tick would let a
# 09:00 window enqueue as late as 17:00.
_DEFAULT_INTERVAL_SECONDS = 600.0


def build_capacity_controller(settings, pool):
    """Build scheduler-only purchasing with token access kept out of workers."""

    if settings.iproyal_mode != "managed":
        return None
    token = settings.iproyal_access_token
    if token is None:
        raise ValueError("IPRoyal managed purchase token unavailable")

    from engagemate.db.proxies import ProxyBindingService, ProxyRepository
    from engagemate.db.proxy_capacity import PostgresCapacityRepository
    from engagemate.proxy.capacity import (
        CapacityPolicy,
        CapacityReconciler,
        parse_geo_catalog,
    )
    from engagemate.proxy.iproyal import IPRoyalProvider
    from engagemate.proxy.iproyal_reseller import IPRoyalResellerClient
    from engagemate.proxy.managed_capacity import ManagedCapacityController

    catalog_setting = settings.iproyal_geo_catalog_json
    if catalog_setting is None:
        raise ValueError("IPRoyal managed geo catalog unavailable")
    repository = PostgresCapacityRepository(pool)
    reconciler = CapacityReconciler(
        repository=repository,
        client=IPRoyalResellerClient(
            token.get_secret_value(),
            timeout_seconds=settings.iproyal_api_timeout_seconds,
        ),
        catalog=parse_geo_catalog(catalog_setting.get_secret_value()),
        policy=CapacityPolicy(
            spare_per_geo=settings.iproyal_spare_per_geo,
            max_order_quantity=settings.iproyal_max_order_quantity,
            max_order_cost_cents=settings.iproyal_max_order_cost_cents,
            max_monthly_cost_cents=settings.iproyal_max_monthly_cost_cents,
            max_trial_leases=settings.iproyal_max_trial_leases,
        ),
    )
    bindings = ProxyBindingService(
        ProxyRepository(pool, settings.session_encryption_key.get_secret_value()),
        IPRoyalProvider.from_settings(settings),
    )
    return ManagedCapacityController(
        settings=settings,
        repository=repository,
        reconciler=reconciler,
        bindings=bindings,
    )


def run_loop(
    scheduler: Scheduler,
    *,
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] | None = None,
    should_continue: Callable[[], bool] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    """Call `enqueue_due` until told to stop, sleeping between ticks."""

    if type(interval_seconds) is not float or interval_seconds <= 0:
        raise ValueError("interval_seconds must be a positive float")

    wait = sleep or time.sleep
    permitted = should_continue or (lambda: True)
    clock = now_fn or (lambda: datetime.now(timezone.utc))

    while permitted():
        try:
            inserted = scheduler.enqueue_due(clock())
            logger.info("scheduler tick enqueued=%d", inserted)
        except Exception:  # noqa: BLE001 — one bad tick must not kill the process
            logger.exception("scheduler tick failed")
        if not permitted():
            break
        wait(interval_seconds)


def interval_seconds() -> float:
    """Return the poll interval, overridable via SCHEDULER_INTERVAL_SECONDS."""

    raw = os.environ.get("SCHEDULER_INTERVAL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            "SCHEDULER_INTERVAL_SECONDS must be a positive number"
        ) from exc
    if value <= 0:
        raise ValueError("SCHEDULER_INTERVAL_SECONDS must be a positive number")
    return value


def main() -> None:  # pragma: no cover - process entrypoint
    """Start the scheduler and stop cleanly on SIGINT or SIGTERM."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from engagemate.config import Settings
    from engagemate.db.pool import create_pool

    settings = Settings()
    pool = create_pool(settings.database_url, min_size=1, max_size=2)
    capacity = build_capacity_controller(settings, pool)
    scheduler = Scheduler(
        pool,
        capacity_tick=capacity.tick if capacity is not None else None,
        require_proxy_binding=capacity is not None,
    )
    stopping = False

    def _stop(_signum: int, _frame: object | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    poll = interval_seconds()
    logger.info("scheduler starting interval_seconds=%.1f", poll)
    try:
        run_loop(
            scheduler,
            interval_seconds=poll,
            should_continue=lambda: not stopping,
        )
    finally:
        pool.close()
        logger.info("scheduler stopped")


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
