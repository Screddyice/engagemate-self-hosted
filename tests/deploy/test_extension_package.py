"""Release gates for the self-hosted Chrome extension.

The versioned archive must match its reviewed source, exclude secret material,
and remain deterministic so an operator can tie the public download to a commit.
Archived Web Store screenshots are excluded; fork owners generate their own package.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LISTING = REPO_ROOT / "docs" / "chrome-web-store-listing.md"
ASSETS = REPO_ROOT / "assets" / "chrome-web-store"

REQUIRED_ASSETS = (
    "01-accounts-install-1280x800.png",
    "02-instagram-sign-in-1280x800.png",
    "03-connected-1280x800.png",
    "promo-small-440x280.png",
)

EXPECTED_ASSET_DIMENSIONS = {
    "01-accounts-install-1280x800.png": (1280, 800),
    "02-instagram-sign-in-1280x800.png": (1280, 800),
    "03-connected-1280x800.png": (1280, 800),
    "promo-small-440x280.png": (440, 280),
}


def test_listing_justifies_every_permission_the_manifest_requests() -> None:
    listing = LISTING.read_text()
    for permission in ("cookies", "storage", "alarms"):
        assert permission in listing
    assert "instagram.com" in listing
    assert "ENGAGEMATE_WEB_ORIGIN" in listing


def test_listing_describes_the_zero_input_connection_flow() -> None:
    listing = LISTING.read_text()
    normalized = " ".join(line.removeprefix("> ").strip() for line in listing.splitlines())
    assert "click Connect Instagram" in normalized
    assert "without asking the user to enter either value" in normalized
    assert "handle the user typed" not in listing
    assert "country they selected" not in listing


def test_listing_carries_the_review_and_asset_contract() -> None:
    listing = LISTING.read_text()
    for field in ("Title", "Summary", "Support URL", "Product URL", "Privacy URL"):
        assert field in listing
    assert "Reviewer note" in listing
    for asset in REQUIRED_ASSETS:
        assert asset in listing


# ─── Deterministic packaging ───────────────────────────────────────────


def build_archive(destination: Path, *origins: str, env: dict | None = None) -> Path:
    command = [sys.executable, str(REPO_ROOT / "scripts" / "package_extension.py")]
    for origin in origins:
        command += ["--origin", origin]
    base = {k: v for k, v in os.environ.items() if k != "ENGAGEMATE_WEB_ORIGIN"}
    subprocess.run(
        command,
        cwd=destination,
        check=True,
        capture_output=True,
        env={**base, **(env or {})},
    )
    [archive] = (destination / "dist").glob("*.zip")
    return archive


def test_archive_is_byte_identical_across_builds(tmp_path: Path) -> None:
    """Two builds of one commit must hash the same, or the reviewed bytes
    cannot be tied to the source they came from."""

    digests = []
    for name in ("first", "second"):
        target = tmp_path / name
        target.mkdir()
        digests.append(hashlib.sha256(build_archive(target).read_bytes()).hexdigest())

    assert digests[0] == digests[1]


def test_archive_is_named_for_the_manifest_version(tmp_path: Path) -> None:
    version = json.loads(
        (REPO_ROOT / "extension" / "manifest.json").read_text()
    )["version"]
    assert build_archive(tmp_path).name == f"engagemate-extension-{version}.zip"


def test_archive_metadata_matches_the_reviewed_store_listing(tmp_path: Path) -> None:
    with zipfile.ZipFile(build_archive(tmp_path)) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    sections = LISTING.read_text().split("\n\n")
    reviewed_fields = {
        label: " ".join(
            section.removeprefix(f"**{label}:** ").splitlines()
        )
        for label in ("Title", "Summary")
        for section in sections
        if section.startswith(f"**{label}:** ")
    }
    assert manifest["name"] == reviewed_fields["Title"]
    assert manifest["description"] == reviewed_fields["Summary"]


def test_archive_ships_only_reviewable_extension_files(tmp_path: Path) -> None:
    with zipfile.ZipFile(build_archive(tmp_path)) as archive:
        names = archive.namelist()

    assert "manifest.json" in names
    assert "content-script.js" in names
    assert "background.js" in names

    for name in names:
        assert not Path(name).name.startswith(".")
        assert Path(name).suffix in {".json", ".js", ".html", ".css", ".png"}
        assert ".map" not in name
        assert "test" not in name.lower()


@pytest.mark.parametrize("secret", ("sessionid", "IGSESSION-", "BEGIN PRIVATE KEY"))
def test_archive_carries_no_session_material(tmp_path: Path, secret: str) -> None:
    with zipfile.ZipFile(build_archive(tmp_path)) as archive:
        for name in archive.namelist():
            body = archive.read(name).decode("utf-8", errors="ignore")
            assert f"{secret}=" not in body


# ─── Trusted origins are a build setting with no public default ────────


def _manifest(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as packaged:
        return json.loads(packaged.read("manifest.json"))


def test_unconfigured_archive_trusts_only_local_development(tmp_path: Path) -> None:
    manifest = _manifest(build_archive(tmp_path))

    web_hosts = [p for p in manifest["host_permissions"] if "instagram.com" not in p]
    assert web_hosts == ["http://localhost:3030/*"]
    assert manifest["content_scripts"][0]["matches"] == ["http://localhost:3030/*"]


def test_configured_origin_replaces_local_development(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path, env={"ENGAGEMATE_WEB_ORIGIN": "https://app.example.com"}
    )
    manifest = _manifest(archive)

    web_hosts = [p for p in manifest["host_permissions"] if "instagram.com" not in p]
    assert web_hosts == ["https://app.example.com/*"]
    assert manifest["content_scripts"][0]["matches"] == ["https://app.example.com/*"]
    with zipfile.ZipFile(archive) as packaged:
        worker = packaged.read("background.js").decode()
    assert 'const ENGAGEMATE_WEB_ORIGINS = ["https://app.example.com"];' in worker


@pytest.mark.parametrize(
    "origin",
    (
        "http://app.example.com",
        "https://*.example.com",
        "https://app.example.com/path",
        "https://user@app.example.com",
    ),
)
def test_unsafe_origin_stops_the_build(tmp_path: Path, origin: str) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        build_archive(tmp_path, origin)
    assert not (tmp_path / "dist").exists()
