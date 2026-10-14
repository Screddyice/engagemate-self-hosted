from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from engagemate.worker.artifact_cleanup import cleanup_artifacts


NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def _job(root: Path, *, age: timedelta) -> Path:
    path = root / str(uuid4()) / str(uuid4())
    path.mkdir(parents=True)
    (path / "01-dom.html").write_text("diagnostic", encoding="utf-8")
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_cleanup_removes_only_artifacts_older_than_seven_days(tmp_path: Path) -> None:
    old = _job(tmp_path, age=timedelta(days=8))
    recent = _job(tmp_path, age=timedelta(days=6, hours=23))

    removed = cleanup_artifacts(tmp_path, now=NOW)

    assert removed == 1
    assert not old.exists()
    assert recent.exists()


def test_cleanup_ignores_non_uuid_and_nested_paths(tmp_path: Path) -> None:
    ignored = tmp_path / "notes" / str(uuid4())
    ignored.mkdir(parents=True)
    stamp = (NOW - timedelta(days=30)).timestamp()
    os.utime(ignored, (stamp, stamp))

    assert cleanup_artifacts(tmp_path, now=NOW) == 0
    assert ignored.exists()


def test_cleanup_refuses_a_symlinked_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        cleanup_artifacts(linked, now=NOW)


def test_cleanup_skips_symlinked_account_and_job_directories(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"outside-{uuid4()}"
    outside.mkdir()
    account_link = tmp_path / str(uuid4())
    account_link.symlink_to(outside, target_is_directory=True)
    account = tmp_path / str(uuid4())
    account.mkdir()
    job_link = account / str(uuid4())
    job_link.symlink_to(outside, target_is_directory=True)

    assert cleanup_artifacts(tmp_path, now=NOW) == 0
    assert outside.exists()


def test_cleanup_requires_an_absolute_root_and_aware_utc_clock(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        cleanup_artifacts(Path("artifacts"), now=NOW)
    with pytest.raises(ValueError, match="clock"):
        cleanup_artifacts(
            tmp_path,
            now=datetime(2026, 9, 3, 8, 0),  # noqa: DTZ001
        )
