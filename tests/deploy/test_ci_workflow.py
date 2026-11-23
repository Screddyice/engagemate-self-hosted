from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict[str, object]:
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def test_ci_runs_for_main_pull_requests_pushes_and_manual_runs() -> None:
    workflow = _workflow()

    assert workflow["on"] == {
        "pull_request": {"branches": ["main"]},
        "push": {"branches": ["main"]},
        "workflow_dispatch": "",
    }
    assert workflow["permissions"] == {"contents": "read"}


def test_github_jobs_wait_for_crow_and_run_when_it_is_silent() -> None:
    jobs = _workflow()["jobs"]
    gate = jobs["crow-gate"]
    script = gate["steps"][0]["run"]

    assert gate["permissions"] == {"statuses": "read"}
    assert 'startswith("ci/crow/")' in script
    assert "seq 1 12" in script and "sleep 15" in script
    assert 'workflow_dispatch' in script
    for name in ("backend", "web"):
        assert jobs[name]["needs"] == "crow-gate"
        assert jobs[name]["if"] == "needs.crow-gate.outputs.run == 'true'"


def test_crow_pipeline_mirrors_the_github_jobs() -> None:
    pipeline = yaml.safe_load((REPO_ROOT / ".woodpecker.yaml").read_text())
    steps = pipeline["steps"]

    assert pipeline["services"]["postgres"]["image"] == "postgres:16-alpine"
    assert "python -m pytest -q" in steps["backend"]["commands"]
    assert {"npm ci", "npm test -- --run", "npm run build", "npx tsc --noEmit"} <= set(
        steps["web"]["commands"]
    )
    assert "linux/amd64" not in (REPO_ROOT / ".woodpecker.yaml").read_text()


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
