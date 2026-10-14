"""Fixed diagnostics for a proxy preflight that stopped before a mutation."""

PREFLIGHT_REASONS = frozenset({
    "proxy_access_denied", "proxy_authentication_failed", "connection_failed",
    "geo_mismatch", "ip_mismatch", "unexpected_status", "invalid_response",
    "no_geo_anchor", "no_geo_readings", "proxy_binding_required",
    "proxy_binding_mismatch",
})


def preflight_failure_evidence(reason: object) -> dict:
    # Reel evidence merges shallowly. Clear prior diagnostics when a later
    # preflight has an unknown reason instead of attributing it to the old one.
    evidence = {"reason": "proxy_or_session_unavailable", "preflight_reason": None,
                "phase": None, "mutation_attempted": False}
    if type(reason) is str and reason in PREFLIGHT_REASONS:
        evidence.update(preflight_reason=reason, phase="proxy_preflight",
                        mutation_attempted=False)
    return evidence
