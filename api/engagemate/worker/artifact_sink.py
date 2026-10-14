"""Persist worker failure diagnostics to a private, tenant-scoped directory.

Redaction belongs to the producer. `InstagramAdapter._capture_best_effort`
masks the page before it screenshots, filters the DOM excerpt down to a fixed
attribute allow list, and strips the URL's query and fragment. This module
owns the destination only, and still treats what arrives as sensitive: every
artifact is derived from a logged-in Instagram session, so it lands under the
run's own account and job in directories no other user can open.

Two rules shape everything here. Path components come from `UUID` values and a
counter, never from anything a page supplied, so no artifact can be written
outside the configured root. And a diagnostic must never change what an action
did, so every failure returns None exactly like the sink that drops everything.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import UUID

from engagemate.worker.instagram_adapter import (
    MAX_DOM_BYTES,
    MAX_SCREENSHOT_BYTES,
    DiagnosticArtifact,
)

MAX_ARTIFACTS_PER_RUN = 5
# The producer slices the URL to this length before handing the artifact over.
MAX_URL_BYTES = 2_048

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_SCREENSHOT_NAME = "screenshot.png"
_DOM_NAME = "dom.html"
_URL_NAME = "url.txt"


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


_DIRECTORY_FLAGS = _directory_flags()


class FilesystemArtifactSink:
    """Write a bounded number of one run's artifacts under `<root>/<account>/<job>`."""

    def __init__(
        self,
        root: Path,
        ig_account_id: UUID,
        job_id: UUID,
        *,
        max_artifacts: int = MAX_ARTIFACTS_PER_RUN,
    ) -> None:
        # Both identifiers become path components, so a crafted string here
        # would be the one way out of the root.
        if type(ig_account_id) is not UUID or type(job_id) is not UUID:
            raise ValueError("artifact scope requires UUID identifiers")
        if type(max_artifacts) is not int or max_artifacts < 0:
            raise ValueError("max_artifacts must not be negative")
        self._root = Path(root)
        self._ig_account_id = ig_account_id
        self._job_id = job_id
        self._max_artifacts = max_artifacts
        self._attempted = 0

    def __repr__(self) -> str:
        return "FilesystemArtifactSink(root=<configured>, scope=<configured>)"

    async def write(self, artifact: DiagnosticArtifact) -> None:
        """Persist one artifact, or nothing at all, and never raise."""

        if self._attempted >= self._max_artifacts:
            return None
        parts = _artifact_parts(artifact)
        if not parts:
            return None
        # Count the attempt, not the success: a root that fails every write
        # must still stop after the cap rather than retry without limit.
        self._attempted += 1
        try:
            self._write_all(self._attempted, parts)
        except OSError:
            # A full disk, a hostile root, or a removed mount is a diagnostic
            # failure, and a diagnostic may not replace the action's result.
            return None
        return None

    def _write_all(
        self, index: int, parts: tuple[tuple[str, bytes], ...]
    ) -> None:
        root_fd = _open_private_root(self._root)
        try:
            account_fd = _open_private_child(root_fd, str(self._ig_account_id))
            try:
                job_fd = _open_private_child(account_fd, str(self._job_id))
                try:
                    for kind, payload in parts:
                        _write_private_file(job_fd, f"{index:02d}-{kind}", payload)
                finally:
                    os.close(job_fd)
            finally:
                os.close(account_fd)
        finally:
            os.close(root_fd)


def _artifact_parts(artifact: object) -> tuple[tuple[str, bytes], ...]:
    """Return the file kinds worth writing, each bounded to the producer's limit."""

    screenshot = getattr(artifact, "screenshot", None)
    excerpt = getattr(artifact, "dom_excerpt", None)
    url = getattr(artifact, "current_url", None)
    screenshot = (
        screenshot[:MAX_SCREENSHOT_BYTES] if type(screenshot) is bytes else b""
    )
    excerpt = _bounded_utf8(excerpt, MAX_DOM_BYTES)
    url = _bounded_utf8(url, MAX_URL_BYTES)
    if not screenshot and not excerpt:
        # A URL on its own names a target without showing what went wrong,
        # so an otherwise empty capture is worth no file at all.
        return ()
    parts: list[tuple[str, bytes]] = []
    if screenshot:
        parts.append((_SCREENSHOT_NAME, screenshot))
    if excerpt:
        parts.append((_DOM_NAME, excerpt))
    if url:
        parts.append((_URL_NAME, url))
    return tuple(parts)


def _bounded_utf8(value: object, limit: int) -> bytes:
    """Encode text to at most `limit` bytes without leaving a split character."""

    if type(value) is not str:
        return b""
    truncated = value.encode("utf-8")[:limit]
    return truncated.decode("utf-8", errors="ignore").encode("utf-8")


def _open_private_root(path: Path) -> int:
    """Create the configured root if it is missing and return a verified handle."""

    os.makedirs(path, mode=_DIRECTORY_MODE, exist_ok=True)
    return _verified_directory(os.open(path, _DIRECTORY_FLAGS))


def _open_private_child(parent_fd: int, name: str) -> int:
    """Create one tenant-scoped subdirectory and return a verified handle."""

    try:
        os.mkdir(name, mode=_DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return _verified_directory(os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd))


def _verified_directory(fd: int) -> int:
    """Refuse anything but a directory this user owns, and tighten its mode.

    `O_NOFOLLOW` already rejects a symlinked component, and `mkdir` applies the
    umask, so the mode is set explicitly rather than assumed.
    """

    secured = False
    try:
        status = os.fstat(fd)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
            raise OSError("artifact directory is not private")
        if stat.S_IMODE(status.st_mode) != _DIRECTORY_MODE:
            os.fchmod(fd, _DIRECTORY_MODE)
        secured = True
    finally:
        if not secured:
            os.close(fd)
    return fd


def _write_private_file(dir_fd: int, name: str, payload: bytes) -> None:
    """Create one artifact file that only this user can read."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with os.fdopen(
        os.open(name, flags, _FILE_MODE, dir_fd=dir_fd), "wb"
    ) as handle:
        os.fchmod(handle.fileno(), _FILE_MODE)
        handle.write(payload)


__all__ = [
    "MAX_ARTIFACTS_PER_RUN",
    "MAX_URL_BYTES",
    "FilesystemArtifactSink",
]
