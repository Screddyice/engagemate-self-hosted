"""The artifact sink writes a logged-in session's diagnostics, so its job is to
stay inside the configured root, stay private, stay bounded, and never turn a
diagnostic failure into a run failure.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from uuid import UUID

import pytest

from engagemate.worker.artifact_sink import (
    MAX_ARTIFACTS_PER_RUN,
    FilesystemArtifactSink,
)
from engagemate.worker.instagram_adapter import DiagnosticArtifact
from engagemate.worker.wiring import DiscardingArtifactSink, artifact_sink_for

ACCOUNT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_ACCOUNT = UUID("22222222-2222-4222-8222-222222222222")
JOB = UUID("33333333-3333-4333-8333-333333333333")


def _artifact(
    screenshot: bytes = b"\x89PNG-bytes",
    excerpt: str = "<main>failed</main>",
    url: str = "https://www.instagram.com/stories/example/",
) -> DiagnosticArtifact:
    return DiagnosticArtifact(screenshot, excerpt, url)


def _write(sink: FilesystemArtifactSink, artifact: DiagnosticArtifact) -> None:
    asyncio.run(sink.write(artifact))


def _run_dir(root: Path, account: UUID = ACCOUNT, job: UUID = JOB) -> Path:
    return root / str(account) / str(job)


def test_artifact_lands_under_its_own_account_and_job(tmp_path: Path) -> None:
    """A shared directory would put one tenant's screenshots beside another's."""

    _write(FilesystemArtifactSink(tmp_path, ACCOUNT, JOB), _artifact())

    written = sorted(p.name for p in _run_dir(tmp_path).iterdir())
    assert written == ["01-dom.html", "01-screenshot.png", "01-url.txt"]
    assert (_run_dir(tmp_path) / "01-screenshot.png").read_bytes() == b"\x89PNG-bytes"


def test_two_accounts_never_share_a_directory(tmp_path: Path) -> None:
    _write(FilesystemArtifactSink(tmp_path, ACCOUNT, JOB), _artifact())
    _write(FilesystemArtifactSink(tmp_path, OTHER_ACCOUNT, JOB), _artifact())

    assert _run_dir(tmp_path).exists()
    assert _run_dir(tmp_path, OTHER_ACCOUNT).exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [str(ACCOUNT), str(OTHER_ACCOUNT)]
    )


@pytest.mark.parametrize(
    "account_id, job_id",
    [
        ("../../etc", JOB),
        (ACCOUNT, "../.."),
        (str(ACCOUNT), JOB),
        (None, JOB),
    ],
)
def test_non_uuid_scope_is_refused_before_anything_is_written(
    tmp_path: Path, account_id: object, job_id: object
) -> None:
    """Both identifiers become path components, so a string is the way out."""

    with pytest.raises(ValueError):
        FilesystemArtifactSink(tmp_path, account_id, job_id)  # type: ignore[arg-type]

    assert list(tmp_path.iterdir()) == []


def test_directories_and_files_are_private(tmp_path: Path) -> None:
    """Another account on the box must not be able to read a session capture."""

    root = tmp_path / "artifacts"
    _write(FilesystemArtifactSink(root, ACCOUNT, JOB), _artifact())

    for directory in (root, root / str(ACCOUNT), _run_dir(root)):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    for written in _run_dir(root).iterdir():
        assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_an_existing_loose_root_is_tightened(tmp_path: Path) -> None:
    root = tmp_path / "loose"
    root.mkdir(mode=0o755)

    _write(FilesystemArtifactSink(root, ACCOUNT, JOB), _artifact())

    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_writes_stop_at_the_cap(tmp_path: Path) -> None:
    """An unbounded sink would fill the disk on a run that fails every story."""

    sink = FilesystemArtifactSink(tmp_path, ACCOUNT, JOB)
    for _ in range(MAX_ARTIFACTS_PER_RUN + 3):
        _write(sink, _artifact())

    screenshots = list(_run_dir(tmp_path).glob("*-screenshot.png"))
    assert len(screenshots) == MAX_ARTIFACTS_PER_RUN


def test_a_failing_root_still_stops_at_the_cap(tmp_path: Path) -> None:
    """Counting successes would let a broken root retry on every story."""

    unwritable = tmp_path / "blocked"
    unwritable.write_text("not a directory")
    sink = FilesystemArtifactSink(unwritable, ACCOUNT, JOB, max_artifacts=2)

    for _ in range(5):
        _write(sink, _artifact())

    assert sink._attempted == 2


def test_an_unwritable_root_never_raises(tmp_path: Path) -> None:
    """A diagnostic may not replace the action result it was describing."""

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    _write(FilesystemArtifactSink(blocked, ACCOUNT, JOB), _artifact())


def test_a_symlinked_root_is_refused(tmp_path: Path) -> None:
    """Following a link would write a session capture wherever it points."""

    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    _write(FilesystemArtifactSink(link, ACCOUNT, JOB), _artifact())

    assert list(target.iterdir()) == []


def test_an_empty_capture_writes_no_file(tmp_path: Path) -> None:
    """A URL alone names a target without showing what went wrong."""

    _write(
        FilesystemArtifactSink(tmp_path, ACCOUNT, JOB),
        _artifact(screenshot=b"", excerpt=""),
    )

    assert list(tmp_path.iterdir()) == []


def test_a_screenshot_alone_is_still_worth_writing(tmp_path: Path) -> None:
    _write(
        FilesystemArtifactSink(tmp_path, ACCOUNT, JOB),
        _artifact(excerpt=""),
    )

    written = sorted(p.name for p in _run_dir(tmp_path).iterdir())
    assert written == ["01-screenshot.png", "01-url.txt"]


def test_filenames_carry_no_page_supplied_text(tmp_path: Path) -> None:
    """A handle or URL in a filename would leak the target through `ls`."""

    _write(
        FilesystemArtifactSink(tmp_path, ACCOUNT, JOB),
        _artifact(url="https://www.instagram.com/stories/secret_handle/"),
    )

    for written in _run_dir(tmp_path).iterdir():
        assert "secret_handle" not in written.name
        assert "instagram" not in written.name


def test_repr_reveals_neither_root_nor_scope(tmp_path: Path) -> None:
    sink = FilesystemArtifactSink(tmp_path / "private-root", ACCOUNT, JOB)

    rendered = repr(sink)
    assert "private-root" not in rendered
    assert str(ACCOUNT) not in rendered
    assert str(JOB) not in rendered


def test_oversized_payloads_are_bounded(tmp_path: Path) -> None:
    from engagemate.worker.instagram_adapter import (
        MAX_DOM_BYTES,
        MAX_SCREENSHOT_BYTES,
    )

    _write(
        FilesystemArtifactSink(tmp_path, ACCOUNT, JOB),
        _artifact(
            screenshot=b"x" * (MAX_SCREENSHOT_BYTES + 4_096),
            excerpt="y" * (MAX_DOM_BYTES + 4_096),
        ),
    )

    run = _run_dir(tmp_path)
    assert (run / "01-screenshot.png").stat().st_size == MAX_SCREENSHOT_BYTES
    assert (run / "01-dom.html").stat().st_size <= MAX_DOM_BYTES


class _Job:
    def __init__(self, ig_account_id: UUID, job_id: UUID) -> None:
        self.ig_account_id = ig_account_id
        self.id = job_id


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_diagnostics_are_off_until_a_directory_is_configured(
    configured: str | None,
) -> None:
    """Collecting nothing is the default, because every artifact is session-derived."""

    sink = artifact_sink_for(configured, _Job(ACCOUNT, JOB))

    assert isinstance(sink, DiscardingArtifactSink)


def test_a_configured_directory_selects_the_filesystem_sink(tmp_path: Path) -> None:
    sink = artifact_sink_for(str(tmp_path), _Job(ACCOUNT, JOB))

    assert isinstance(sink, FilesystemArtifactSink)

    asyncio.run(sink.write(_artifact()))
    assert _run_dir(tmp_path).exists()


def test_the_discarding_sink_writes_nothing_anywhere(tmp_path: Path) -> None:
    asyncio.run(DiscardingArtifactSink().write(_artifact()))

    assert list(tmp_path.iterdir()) == []


def test_a_negative_cap_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FilesystemArtifactSink(tmp_path, ACCOUNT, JOB, max_artifacts=-1)


def test_a_zero_cap_writes_nothing(tmp_path: Path) -> None:
    _write(FilesystemArtifactSink(tmp_path, ACCOUNT, JOB, max_artifacts=0), _artifact())

    assert list(tmp_path.iterdir()) == []


def test_the_root_is_created_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "artifacts"

    _write(FilesystemArtifactSink(root, ACCOUNT, JOB), _artifact())

    assert _run_dir(root).exists()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_a_second_run_of_the_same_account_gets_its_own_directory(
    tmp_path: Path,
) -> None:
    second_job = UUID("44444444-4444-4444-8444-444444444444")

    _write(FilesystemArtifactSink(tmp_path, ACCOUNT, JOB), _artifact())
    _write(FilesystemArtifactSink(tmp_path, ACCOUNT, second_job), _artifact())

    account_dir = tmp_path / str(ACCOUNT)
    assert sorted(p.name for p in account_dir.iterdir()) == sorted(
        [str(JOB), str(second_job)]
    )


def test_umask_cannot_loosen_a_new_directory(tmp_path: Path) -> None:
    """mkdir applies the umask, so the mode is set rather than assumed."""

    previous = os.umask(0)
    try:
        _write(FilesystemArtifactSink(tmp_path / "root", ACCOUNT, JOB), _artifact())
    finally:
        os.umask(previous)

    assert stat.S_IMODE((tmp_path / "root").stat().st_mode) == 0o700
