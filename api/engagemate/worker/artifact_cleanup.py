"""Expire tenant-scoped worker diagnostics after seven days."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

_RETENTION = timedelta(days=7)


def cleanup_artifacts(
    root: Path,
    *,
    now: datetime | None = None,
) -> int:
    """Delete expired direct `<account>/<job>` directories below `root`."""

    root = Path(root)
    if not root.is_absolute():
        raise ValueError("artifact root must be absolute")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("artifact cleanup clock must be timezone-aware")
    current = current.astimezone(UTC)
    try:
        root_status = root.lstat()
    except FileNotFoundError:
        return 0
    if root.is_symlink():
        raise ValueError("artifact root must not be a symlink")
    if not root.is_dir() or root_status.st_uid != os.geteuid():
        raise ValueError("artifact root is not a directory owned by this user")
    resolved_root = root.resolve(strict=True)
    cutoff = (current - _RETENTION).timestamp()
    removed = 0

    for account in root.iterdir():
        if not _private_uuid_directory(account, resolved_root):
            continue
        resolved_account = account.resolve(strict=True)
        for job in account.iterdir():
            if not _private_uuid_directory(job, resolved_account):
                continue
            resolved_job = job.resolve(strict=True)
            if resolved_job.parent != resolved_account:
                continue
            if job.lstat().st_mtime >= cutoff:
                continue
            shutil.rmtree(job)
            removed += 1
    return removed


def _private_uuid_directory(path: Path, expected_parent: Path) -> bool:
    try:
        UUID(path.name)
        status = path.lstat()
        if path.is_symlink() or not path.is_dir() or status.st_uid != os.geteuid():
            return False
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError, ValueError):
        return False
    return resolved.parent == expected_parent


def main() -> None:  # pragma: no cover - systemd entrypoint
    configured = os.environ.get("ARTIFACT_DIR", "").strip()
    if not configured:
        return
    cleanup_artifacts(Path(configured))


if __name__ == "__main__":  # pragma: no cover
    main()
