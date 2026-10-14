from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict[str, object]:
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def test_ci_runs_for_main_pull_requests_and_pushes() -> None:
    workflow = _workflow()

    assert workflow["on"] == {
        "pull_request": {"branches": ["main"]},
        "push": {"branches": ["main"]},
    }
    assert workflow["permissions"] == {"contents": "read"}


def test_backend_ci_runs_the_full_suite_with_postgres() -> None:
    backend = _workflow()["jobs"]["backend"]
    postgres = backend["services"]["postgres"]
    commands = "\n".join(
        step.get("run", "") for step in backend["steps"] if isinstance(step, dict)
    )

    assert postgres["image"] == "postgres:16-alpine"
    assert backend["env"]["PGCLIENTENCODING"] == "UTF8"
    assert "python -m pip install -r api/requirements-test.txt" in commands
    assert "python -m pytest -q" in commands


def test_web_ci_runs_tests_build_and_typecheck_with_postgres() -> None:
    web = _workflow()["jobs"]["web"]
    postgres = web["services"]["postgres"]
    commands = "\n".join(
        step.get("run", "") for step in web["steps"] if isinstance(step, dict)
    )

    assert postgres["image"] == "postgres:16-alpine"
    assert web["env"]["PGCLIENTENCODING"] == "UTF8"
    assert "NEXT_PUBLIC_APP_URL" not in web["env"]
    assert "npm ci" in commands
    assert "npm test -- --run" in commands
    assert commands.index("npm run build") < commands.index("npx tsc --noEmit")
