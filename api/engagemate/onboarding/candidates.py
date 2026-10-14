"""Balanced selection for candidates discovered from several audience sources."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from engagemate.onboarding.analysis import score_search_results


def select_balanced_candidates(
    groups: list[tuple[str, list[dict[str, Any]]]],
    *,
    reference_usernames: set[str],
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return qualified candidates without allowing one source to dominate."""

    if limit <= 0:
        return []

    references = {username.strip().lower() for username in reference_usernames}
    source_order: list[str] = []
    queues: dict[str, list[str]] = {}
    best_by_username: dict[str, dict[str, Any]] = {}
    sources_by_username: dict[str, set[str]] = defaultdict(set)

    for raw_source, rows in groups:
        source = raw_source.strip().lower()
        if not source:
            continue
        if source not in queues:
            source_order.append(source)
            queues[source] = []

        for candidate in score_search_results(rows):
            username = str(candidate["username"]).strip().lower()
            if (
                not candidate["accepted"]
                or username in references
                or username in queues[source]
            ):
                continue
            queues[source].append(username)
            sources_by_username[username].add(source)
            current = best_by_username.get(username)
            if current is None or int(candidate["score"]) > int(current["score"]):
                best_by_username[username] = candidate

    for username, candidate in best_by_username.items():
        candidate["sources"] = sorted(sources_by_username[username])

    selected: list[dict[str, Any]] = []
    selected_usernames: set[str] = set()
    while len(selected) < limit:
        added = False
        for source in source_order:
            queue = queues[source]
            while queue and queue[0] in selected_usernames:
                queue.pop(0)
            if not queue:
                continue
            username = queue.pop(0)
            selected.append(best_by_username[username])
            selected_usernames.add(username)
            added = True
            if len(selected) == limit:
                break
        if not added:
            break

    remaining = sorted(
        (
            candidate
            for username, candidate in best_by_username.items()
            if username not in selected_usernames
        ),
        key=lambda candidate: (-int(candidate["score"]), str(candidate["username"])),
    )
    selected.extend(remaining[: max(limit - len(selected), 0)])
    return selected
