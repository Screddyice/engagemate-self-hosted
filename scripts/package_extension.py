#!/usr/bin/env python3
"""Build the self-hosted archive for the EngageMate Chrome extension.

A store upload is reviewed as bytes, but the thing anyone can audit later is the
commit. Those only line up if the archive is reproducible, so this script sorts
its entries and pins every timestamp: two runs of one commit produce identical
bytes, and a mismatch means the tree changed.

It is also a whitelist. Only the file types Chrome actually loads go in, which
keeps a stray .env, a source map, or an editor backup out of a public archive.

    python3 scripts/package_extension.py
    ENGAGEMATE_WEB_ORIGIN=https://app.example.com python3 scripts/package_extension.py

The extension trusts exactly the web origins it is built for. The committed
source trusts only http://localhost:3030. Set ENGAGEMATE_WEB_ORIGIN (one origin,
or several separated by commas, primary first) or pass --origin to build for a
deployment; the script then rewrites the manifest's host permissions, the
content-script matches, and the worker's origin list to those origins only.
There is no default public origin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = Path(__file__).resolve().parents[1] / "extension"

# Everything Chrome loads, and nothing else.
ALLOWED_SUFFIXES = {".json", ".js", ".html", ".css", ".png"}

# A store archive is public. Anything here has no business being public.
FORBIDDEN_PARTS = ("test", "spec", "fixture", "__pycache__", "node_modules")
FORBIDDEN_SUFFIXES = (".map", ".log", ".env", ".key", ".pem")

MAX_FILE_BYTES = 5 * 1024 * 1024

ORIGIN_ENV = "ENGAGEMATE_WEB_ORIGIN"
DEV_ORIGIN = "http://localhost:3030"
DEV_PATTERN = f"{DEV_ORIGIN}/*"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
ORIGINS_LINE = re.compile(r"^const ENGAGEMATE_WEB_ORIGINS = \[.*\];$", re.MULTILINE)

# Zip's DOS timestamp epoch. Any fixed value works; this is the earliest legal
# one, so it reads as deliberate rather than as a real capture time.
PINNED_TIME = (1980, 1, 1, 0, 0, 0)


def should_include(path: Path) -> bool:
    """Decide whether one file belongs in a public, reviewable archive."""

    relative = path.relative_to(SOURCE)

    if any(part.startswith(".") for part in relative.parts):
        return False
    if path.suffix not in ALLOWED_SUFFIXES:
        return False
    if path.suffix in FORBIDDEN_SUFFIXES:
        return False

    lowered = str(relative).lower()
    if any(marker in lowered for marker in FORBIDDEN_PARTS):
        return False
    if path.stat().st_size > MAX_FILE_BYTES:
        return False

    return True


def normalize_origin(value: str) -> str:
    """Accept a bare https origin, or http on loopback, and nothing else."""

    parsed = urlsplit(value.strip())
    host = parsed.hostname
    if not host:
        raise SystemExit(f"{ORIGIN_ENV}: {value!r} has no host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit(f"{ORIGIN_ENV}: {value!r} must be a bare origin")
    if parsed.path not in {"", "/"} or "*" in parsed.netloc:
        raise SystemExit(f"{ORIGIN_ENV}: {value!r} must be a bare origin without wildcards")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and host in LOOPBACK_HOSTS):
        raise SystemExit(f"{ORIGIN_ENV}: {value!r} must use HTTPS outside loopback")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def configured_origins(cli: list[str] | None) -> list[str]:
    """Origins from --origin, else ENGAGEMATE_WEB_ORIGIN, else none."""

    raw = cli if cli else (os.environ.get(ORIGIN_ENV) or "").split(",")
    origins = [normalize_origin(item) for item in raw if item.strip()]
    return list(dict.fromkeys(origins))


def configure_manifest(source: bytes, origins: list[str]) -> bytes:
    """Swap the local development pattern for the configured origins."""

    manifest = json.loads(source)
    patterns = [f"{origin}/*" for origin in origins]

    def swap(entries: list[str]) -> list[str]:
        if DEV_PATTERN not in entries:
            raise SystemExit(f"manifest.json no longer lists {DEV_PATTERN}; cannot configure origins")
        result: list[str] = []
        for entry in entries:
            result.extend(patterns if entry == DEV_PATTERN else [entry])
        return list(dict.fromkeys(result))

    manifest["host_permissions"] = swap(manifest["host_permissions"])
    for script in manifest.get("content_scripts", []):
        script["matches"] = swap(script["matches"])
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def configure_worker(source: bytes, origins: list[str]) -> bytes:
    """Rewrite the worker's single origin list line."""

    text = source.decode("utf-8")
    line = f"const ENGAGEMATE_WEB_ORIGINS = {json.dumps(origins)};"
    text, count = ORIGINS_LINE.subn(line, text)
    if count != 1:
        raise SystemExit("background.js must declare ENGAGEMATE_WEB_ORIGINS on exactly one line")
    return text.encode("utf-8")


def file_bytes(path: Path, origins: list[str]) -> bytes:
    body = path.read_bytes()
    if not origins:
        return body
    relative = path.relative_to(SOURCE).as_posix()
    if relative == "manifest.json":
        return configure_manifest(body, origins)
    if relative == "background.js":
        return configure_worker(body, origins)
    return body


def collect() -> list[Path]:
    return sorted(
        (path for path in SOURCE.rglob("*") if path.is_file() and should_include(path)),
        key=lambda path: str(path.relative_to(SOURCE)),
    )


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Package the EngageMate extension.")
    parser.add_argument(
        "--origin",
        action="append",
        help=f"Trusted web origin (repeatable). Overrides {ORIGIN_ENV}.",
    )
    arguments = parser.parse_args(argv)
    origins = configured_origins(arguments.origin)

    version = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))["version"]

    destination = Path("dist") / f"engagemate-extension-{version}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    files = collect()
    if not any(path.name == "manifest.json" for path in files):
        raise SystemExit("extension/manifest.json is missing; refusing to package")

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(
                str(path.relative_to(SOURCE)).replace("\\", "/"),
                date_time=PINNED_TIME,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, file_bytes(path, origins))

    trusted = ", ".join(origins) if origins else f"{DEV_ORIGIN} only (set {ORIGIN_ENV} for a deployment)"
    print(f"{destination} ({len(files)} files); trusts {trusted}")
    return destination


if __name__ == "__main__":
    main()
