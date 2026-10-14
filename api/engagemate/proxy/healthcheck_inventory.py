"""Check every unbound spare lease through its own proxy, before an account does.

Nothing checked inventory until an account tried to bind to it. That put the
first real test of a lease inside a live Instagram run: `ensure_binding` created
the binding, failed the health check, and left the account holding a proxy it
could not use. When the geography had no spare, the rebind failed too and the
account simply never ran.

This command moves that check earlier. It reports what each lease actually is,
and with `--update` records the result so `_claim_managed_inventory` stops
handing out leases that cannot pass.

Prints geography and host only. Credentials are decrypted in memory to build the
request and never reach stdout, a log, or an exception message.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Protocol

from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.db.proxy_capacity import InventoryProxy, PostgresCapacityRepository
from engagemate.proxy.base import ProxyEndpoint, ProxyHealth, ProxyHealthStatus
from engagemate.proxy.iproyal import IPRoyalProvider
from engagemate.security.session_cipher import SessionCipher, SessionCipherError

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_EMPTY = 2


class CheckPort(Protocol):
    def check(self, endpoint: ProxyEndpoint) -> ProxyHealth: ...


class RepositoryPort(Protocol):
    def list_unbound_inventory(self) -> tuple[InventoryProxy, ...]: ...

    def mark_inventory_health(self, proxy_id: object, healthy: bool) -> bool: ...


def _decrypt(proxy: InventoryProxy, encryption_key: str) -> tuple[str, str]:
    cipher = SessionCipher(encryption_key, proxy.id)
    username = cipher.decrypt(proxy.encrypted_username).get("value")
    password = cipher.decrypt(proxy.encrypted_password).get("value")
    if not isinstance(username, str) or not isinstance(password, str):
        raise SessionCipherError("stored proxy credential is invalid")
    return username, password


def check_inventory(
    repository: RepositoryPort,
    provider: CheckPort,
    encryption_key: str,
    *,
    update: bool = False,
) -> tuple[int, int, int]:
    """Return `(healthy, degraded, recorded)` across every unbound spare lease.

    `recorded` counts rows written, which with `update` is every lease reached.
    """

    healthy = degraded = updated = 0
    for proxy in repository.list_unbound_inventory():
        label = f"{proxy.geo.country}/{proxy.geo.region}/{proxy.geo.city}"
        try:
            username, password = _decrypt(proxy, encryption_key)
        # A lease whose credentials will not decrypt cannot be used, and the
        # reason must not be printed: the exception can carry ciphertext.
        except (SessionCipherError, ValueError, TypeError):
            degraded += 1
            logger.error("%s %s undecryptable", label, proxy.host)
            if update and repository.mark_inventory_health(proxy.id, False):
                updated += 1
            continue

        endpoint = ProxyEndpoint(
            provider=proxy.provider,
            provider_reference=proxy.provider_reference,
            proxy_type=proxy.proxy_type,
            geo=proxy.geo,
            host=proxy.host,
            port=proxy.port,
            username=username,
            password=password,
            id=proxy.id,
        )
        health = provider.check(endpoint)
        is_healthy = health.status is ProxyHealthStatus.HEALTHY
        if is_healthy:
            healthy += 1
            logger.info("%s %s healthy", label, proxy.host)
        else:
            degraded += 1
            logger.error("%s %s %s", label, proxy.host, health.reason)
        # Record every result, including an unchanged one. last_checked_at is
        # how you tell a lease that passed from a lease nobody ever dialled,
        # and writing only on a transition leaves those two indistinguishable.
        if update and repository.mark_inventory_health(proxy.id, is_healthy):
            updated += 1
    return healthy, degraded, updated


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entrypoint
    parser = argparse.ArgumentParser(
        description="Health-check unbound proxy inventory."
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help=(
            "Record each result. A degraded lease stops being handed to an "
            "account until a later run finds it healthy again."
        ),
    )
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings()
    if settings.iproyal_mode == "disabled":
        logger.error("IPROYAL_MODE is disabled; there is no inventory to check")
        return EXIT_EMPTY

    pool = create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        healthy, degraded, updated = check_inventory(
            PostgresCapacityRepository(pool),
            IPRoyalProvider.from_settings(settings),
            settings.session_encryption_key.get_secret_value(),
            update=arguments.update,
        )
    finally:
        pool.close()

    logger.info(
        "checked healthy=%d degraded=%d updated=%d", healthy, degraded, updated
    )
    if healthy == 0 and degraded == 0:
        logger.error("no unbound inventory found")
        return EXIT_EMPTY
    return EXIT_DEGRADED if degraded else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
