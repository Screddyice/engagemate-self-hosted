"""Scheduler-owned managed proxy purchasing and assignment."""

from __future__ import annotations

from engagemate.config import Settings
from engagemate.db.proxies import ProxyBindingError, ProxyBindingService
from engagemate.db.proxy_capacity import PostgresCapacityRepository
from engagemate.proxy.capacity import CapacityReconciler


class ManagedCapacityController:
    """Refresh orders, buy bounded shortfalls, then bind ready inventory."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: PostgresCapacityRepository,
        reconciler: CapacityReconciler,
        bindings: ProxyBindingService,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._reconciler = reconciler
        self._bindings = bindings

    def tick(self) -> int:
        key = self._settings.session_encryption_key.get_secret_value()
        changed = self._reconciler.refresh_submitted(key)
        changed += self._reconciler.reconcile()
        for account in self._repository.list_accounts_needing_binding():
            try:
                self._bindings.ensure_binding(
                    account.user_id, account.account_id, account.geo
                )
            except ProxyBindingError:
                try:
                    self._bindings.rebind(
                        account.user_id,
                        account.account_id,
                        account.geo,
                        reason="managed_proxy_health_recovery",
                    )
                except ProxyBindingError:
                    continue
            changed += 1
        return changed
