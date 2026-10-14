from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_BRAND_PATHS = (
    REPOSITORY_ROOT / "api" / ".env.example",
    REPOSITORY_ROOT / "app" / "src",
    REPOSITORY_ROOT / "deploy",
    REPOSITORY_ROOT / "extension",
)


def test_active_product_surfaces_do_not_reference_wriqer() -> None:
    offenders: list[str] = []
    for path in ACTIVE_BRAND_PATHS:
        files = [path] if path.is_file() else path.rglob("*")
        for candidate in files:
            if not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "wriqer" in content.lower():
                offenders.append(str(candidate.relative_to(REPOSITORY_ROOT)))

    assert offenders == []
