from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

GEO_REGISTRY = json.dumps(
    [
        {
            "country": "IN",
            "region": "MH",
            "city": "Mumbai",
            "timezone": "Asia/Kolkata",
            "locale": "en-IN",
        }
    ]
)
LEASE_INVENTORY = json.dumps(
    [
        {
            "reference": "mumbai-test-1",
            "host": "203.0.113.10",
            "port": 12345,
            "username": "proxy-user",
            "password": "proxy-password",
            "country": "IN",
            "region": "MH",
            "city": "Mumbai",
        }
    ]
)
GEO_CATALOG = json.dumps(
    [
        {
            "country": "IN",
            "region": "MH",
            "city": "Mumbai",
            "product_id": 9,
            "plan_id": 4,
            "location_id": 51,
        }
    ]
)


def _runtime_environment() -> dict[str, str]:
    return {
        "ENGAGEMATE_DB_PASSWORD": "synthetic-database-password",
        "DATABASE_URL": "postgresql://engagemate:test@db.invalid/engagemate",
        "INTERNAL_API_KEY": "I" * 32,
        "SESSION_ENCRYPTION_KEY": "A" * 43,
        "FINGERPRINT_SEED": "fingerprint-seed-that-is-at-least-32-bytes",
        "FINGERPRINT_GEO_MAP_JSON": GEO_REGISTRY,
        "INTELLIGENCE_PROVIDER": "hermes_agent",
        "HERMES_AGENT_URL": "http://127.0.0.1:8643/v1/chat/completions",
        "HERMES_AGENT_API_KEY": "H" * 48,
        "HERMES_AGENT_MODEL": "engagemate-agent",
        "HERMES_AGENT_TIMEOUT_SECONDS": "60",
        "HERMES_EXECUTION_TIMEOUT_SECONDS": "1800",
        "IPROYAL_MODE": "import",
        "IPROYAL_ISP_LEASES_JSON": LEASE_INVENTORY,
        "IPROYAL_GEO_CATALOG_JSON": GEO_CATALOG,
        "IPROYAL_ACCESS_TOKEN": "provider-purchase-token",
        "PROXY_HEALTH_URL": "https://identity.example.invalid/v1/check",
        "PROXY_CONNECT_TIMEOUT_SECONDS": "5",
        "PROXY_READ_TIMEOUT_SECONDS": "10",
        "HYPERCRAWL_URL": "https://hypercrawl.example.invalid",
        "HYPERCRAWL_REST_TOKEN": "hypercrawl-test-token",
        "GROWTH_SNAPSHOT_BATCH_SIZE": "25",
        "GROWTH_SNAPSHOT_CONCURRENCY": "4",
        "RESEND_API_KEY": "re_test_private_key",
        "WEEKLY_REPORT_FROM": "reports@example.test",
        "WEEKLY_REPORT_BATCH_SIZE": "25",
        "WEEKLY_REPORT_CANARY_USER_ID": "",
        "WEEKLY_REPORT_CANARY_RECIPIENT": "",
        "CLERK_SECRET_KEY": "clerk-test-key",
        "CLERK_WEBHOOK_SECRET": "clerk-webhook-test-key",
        "STRIPE_SECRET_KEY": "stripe-test-key",
        "STRIPE_WEBHOOK_SECRET": "stripe-webhook-test-key",
        "STRIPE_STARTER_PRICE_ID": "price_starter",
        "STRIPE_PRO_PRICE_ID": "price_pro",
        "STRIPE_SCALE_PRICE_ID": "price_scale",
        "STRIPE_AGENCY_PRICE_ID": "price_agency",
        "STRIPE_CUSTOM_ACCOUNT_PRICE_ID": "price_custom_account",
        "STRIPE_OVERAGE_PRICE_ID": "price_overage",
        "STRIPE_OVERAGE_METER_EVENT_NAME": "engagement_units",
        "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": "pk_test_clerk",
        "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_test_stripe",
        "NEXT_PUBLIC_APP_URL": "http://localhost:3030",
        "AGENT_API_KEY": "A" * 32,
        "CONTROL_API_URL": "http://api:8000",
    }


def _compose_config(tmp_path: Path) -> dict[str, object]:
    shutil.copy2(REPO_ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    environment = _runtime_environment()
    api_names = (
        "DATABASE_URL",
        "INTERNAL_API_KEY",
        "SESSION_ENCRYPTION_KEY",
        "FINGERPRINT_SEED",
        "FINGERPRINT_GEO_MAP_JSON",
        "IPROYAL_MODE",
        "IPROYAL_ISP_LEASES_JSON",
        "IPROYAL_GEO_CATALOG_JSON",
        "PROXY_HEALTH_URL",
        "PROXY_CONNECT_TIMEOUT_SECONDS",
        "PROXY_READ_TIMEOUT_SECONDS",
        "HYPERCRAWL_URL",
        "HYPERCRAWL_REST_TOKEN",
        "GROWTH_SNAPSHOT_BATCH_SIZE",
        "GROWTH_SNAPSHOT_CONCURRENCY",
        "RESEND_API_KEY",
        "WEEKLY_REPORT_FROM",
        "WEEKLY_REPORT_BATCH_SIZE",
        "WEEKLY_REPORT_CANARY_USER_ID",
        "WEEKLY_REPORT_CANARY_RECIPIENT",
    )
    worker_names = (
        "INTELLIGENCE_PROVIDER",
        "HERMES_AGENT_URL",
        "HERMES_AGENT_API_KEY",
        "HERMES_AGENT_MODEL",
        "HERMES_EXECUTION_TIMEOUT_SECONDS",
    )
    scheduler_names = ("IPROYAL_ACCESS_TOKEN",)
    web_names = (
        "DATABASE_URL",
        "CLERK_SECRET_KEY",
        "CLERK_WEBHOOK_SECRET",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_STARTER_PRICE_ID",
        "STRIPE_PRO_PRICE_ID",
        "STRIPE_SCALE_PRICE_ID",
        "STRIPE_AGENCY_PRICE_ID",
        "STRIPE_CUSTOM_ACCOUNT_PRICE_ID",
        "STRIPE_OVERAGE_PRICE_ID",
        "STRIPE_OVERAGE_METER_EVENT_NAME",
        "AGENT_API_KEY",
        "CONTROL_API_URL",
        "INTERNAL_API_KEY",
    )
    (deploy / ".env.api").write_text(
        "".join(f"{name}={environment[name]}\n" for name in api_names),
        encoding="utf-8",
    )
    (deploy / ".env.web").write_text(
        "".join(f"{name}={environment[name]}\n" for name in web_names),
        encoding="utf-8",
    )
    (deploy / ".env.worker").write_text(
        "".join(f"{name}={environment[name]}\n" for name in worker_names),
        encoding="utf-8",
    )
    (deploy / ".env.scheduler").write_text(
        "".join(f"{name}={environment[name]}\n" for name in scheduler_names),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--profile", "email", "--profile", "browser-login",
            "--file",
            str(tmp_path / "docker-compose.yml"),
            "--project-directory",
            str(tmp_path),
            "config",
            "--format",
            "json",
        ],
        cwd=tmp_path,
        env={**os.environ, **environment},
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_compose_isolates_worker_and_scheduler_behind_one_backend_image(
    tmp_path: Path,
) -> None:
    """Dropping a service, secret, or port isolation must break deployment CI."""

    config = _compose_config(tmp_path)
    services = config["services"]
    assert isinstance(services, dict)
    assert set(services) == {"db", "web", "api", "worker", "scheduler", "action-emails", "login"}

    api = services["api"]
    worker = services["worker"]
    scheduler = services["scheduler"]
    web = services["web"]
    assert isinstance(api, dict)
    assert isinstance(worker, dict)
    assert isinstance(scheduler, dict)
    assert isinstance(web, dict)
    assert "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY" not in web["build"]["args"]
    # Optional build arg: an unset host value resolves to empty, not an error.


    assert worker["build"] == api["build"]
    assert scheduler["build"] == api["build"]
    assert api["command"] == [
        "uvicorn",
        "main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    assert worker["command"] == ["python", "-m", "engagemate.worker.main"]
    assert scheduler["command"] == ["python", "-m", "engagemate.scheduler.main"]
    assert "ports" not in worker
    assert "ports" not in scheduler
    assert [port["published"] for port in api["ports"]] == ["13100"]
    assert [port["host_ip"] for port in api["ports"]] == ["127.0.0.1"]
    assert worker["stop_grace_period"] == "45s"

    for backend in (api, scheduler):
        assert (
            backend["environment"]["DATABASE_URL"]
            == _runtime_environment()["DATABASE_URL"]
        )
    assert (
        api["environment"]["FINGERPRINT_GEO_MAP_JSON"]
        == worker["environment"]["FINGERPRINT_GEO_MAP_JSON"]
    )
    for name in (
        "SESSION_ENCRYPTION_KEY",
        "FINGERPRINT_SEED",
        "IPROYAL_ISP_LEASES_JSON",
    ):
        assert worker["environment"][name] == _runtime_environment()[name]
    for name in (
        "INTELLIGENCE_PROVIDER",
        "HERMES_AGENT_URL",
        "HERMES_AGENT_API_KEY",
        "HERMES_AGENT_MODEL",
    ):
        assert worker["environment"][name] == _runtime_environment()[name]
        assert name not in api["environment"]
        assert name not in scheduler["environment"]

    assert (
        scheduler["environment"]["IPROYAL_ACCESS_TOKEN"]
        == (_runtime_environment()["IPROYAL_ACCESS_TOKEN"])
    )
    assert "IPROYAL_ACCESS_TOKEN" not in api["environment"]
    assert "IPROYAL_ACCESS_TOKEN" not in worker["environment"]

    assert worker["network_mode"] == "host"
    assert "volumes" not in api
    assert "volumes" not in scheduler

    # The worker may keep failure diagnostics, and only those. A named volume
    # is Docker-managed, so it preserves the property this contract protects:
    # no service reaches the host filesystem. A bind mount would break it, so
    # every mount here has to be type "volume", never "bind".
    worker_mounts = worker.get("volumes", [])
    assert [
        (mount["type"], mount["source"], mount["target"]) for mount in worker_mounts
    ] == [("volume", "engagemate-artifacts", "/srv/artifacts")]
    assert config["volumes"]["engagemate-artifacts"] is not None

    assert "healthcheck" in api
    assert "healthcheck" in web


def test_every_service_caps_memory_processes_and_cpu(tmp_path: Path) -> None:
    """The caps live in the repo so a deploy cannot quietly remove them.

    These four numbers were applied by hand on the live host on 2026-08-25 and
    were still running on every container on 2026-08-26, but they existed in no
    commit and no open PR. Because deploy.sh rsyncs with --delete, the next
    routine deploy would have overwritten docker-compose.yml with a copy that
    had no caps at all, recreated all four containers unbounded, and reported
    success -- the healthchecks only ask whether a service answers.

    An unbounded worker is the one that matters: it drives headless Chromium,
    so a leak there takes the host down with it rather than dying alone.

    The worker cap covers active Chromium workloads, not just idle Python RSS.
    Changing a cap deliberately means changing it here so it cannot happen
    by accident.
    """

    services = _compose_config(tmp_path)["services"]
    assert isinstance(services, dict)

    # mem_limit comes back from `docker compose config` in bytes, as a string.
    expected = {
        "db": {"mem_limit": "536870912", "pids_limit": 128, "cpus": 1.0},
        "web": {"mem_limit": "805306368", "pids_limit": 256, "cpus": 1.5},
        # One serialized onboarding profile browser now runs inside the API.
        "api": {"mem_limit": "1073741824", "pids_limit": 256, "cpus": 1.5},
        "worker": {"mem_limit": "2147483648", "pids_limit": 256, "cpus": 1.5},
        "scheduler": {"mem_limit": "268435456", "pids_limit": 128, "cpus": 0.5},
        "action-emails": {"mem_limit": "268435456", "pids_limit": 64, "cpus": 0.5},
        "login": {"mem_limit": "1610612736", "pids_limit": 256, "cpus": 1.5},
    }
    assert set(services) == set(expected)
    assert services["worker"]["deploy"]["replicas"] == 1

    for name, caps in expected.items():
        service = services[name]
        for field, value in caps.items():
            assert service.get(field) == value, (
                f"{name}.{field} is {service.get(field)!r}, expected {value!r}; "
                "production runs these caps, so dropping one here means the "
                "next deploy silently unbounds a live container"
            )


def test_artifact_dir_template_points_inside_the_mounted_volume() -> None:
    """A path outside the mount discards evidence without ever erroring.

    The worker had ARTIFACT_DIR blank in production, so a live Instagram run
    that hit a checkpoint left no screenshot behind. Blank is a valid setting;
    a plausible-looking path that is not mounted is the trap worth failing on.
    """

    template = (REPO_ROOT / "deploy" / ".env.api.example").read_text(
        encoding="utf-8"
    )
    configured = next(
        line.split("=", 1)[1].strip()
        for line in template.splitlines()
        if line.startswith("ARTIFACT_DIR=")
    )
    assert configured == "/srv/artifacts"

    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"engagemate-artifacts:{configured}" in compose


def test_api_compose_service_supplies_the_one_shot_reporting_environment(
    tmp_path: Path,
) -> None:
    """The timer command must reuse the API image and its mode-0600 env file."""

    config = _compose_config(tmp_path)
    services = config["services"]
    assert isinstance(services, dict)
    api = services["api"]
    assert isinstance(api, dict)

    expected = _runtime_environment()
    for name in (
        "DATABASE_URL",
        "HYPERCRAWL_URL",
        "HYPERCRAWL_REST_TOKEN",
        "GROWTH_SNAPSHOT_BATCH_SIZE",
        "GROWTH_SNAPSHOT_CONCURRENCY",
        "RESEND_API_KEY",
        "WEEKLY_REPORT_FROM",
        "WEEKLY_REPORT_BATCH_SIZE",
        "WEEKLY_REPORT_CANARY_USER_ID",
        "WEEKLY_REPORT_CANARY_RECIPIENT",
    ):
        assert api["environment"][name] == expected[name]

    assert api["build"] == services["worker"]["build"]
    assert api["build"] == services["scheduler"]["build"]


def test_backend_image_does_not_embed_an_inference_client_or_credentials() -> None:
    """Inference belongs to the host Hermes profile, not the worker image."""

    dockerfile = (REPO_ROOT / "api" / "Dockerfile").read_text(encoding="utf-8")
    assert "@openai/codex" not in dockerfile
    assert "auth.json" not in dockerfile


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"IPROYAL_ISP_LEASES_JSON": ""}, "lease inventory"),
        ({"PROXY_HEALTH_URL": "http://identity.invalid"}, "HTTPS"),
        ({"FINGERPRINT_SEED": "short"}, "fingerprint seed"),
        ({"FINGERPRINT_GEO_MAP_JSON": "{}"}, "geography registry"),
        ({"INTELLIGENCE_PROVIDER": "anthropic"}, "Hermes agent"),
        (
            # Still refused, and now for the sharper reason: a NAME is rejected
            # outright rather than resolved, because a name that points
            # somewhere private today can be repointed after the check passes.
            {
                "HERMES_AGENT_URL": "http://host.docker.internal:8643/v1/chat/completions"
            },
            "literal IP address",
        ),
        (
            # A literal public address is refused by the private-address rule.
            {"HERMES_AGENT_URL": "http://8.8.8.8:8643/v1/chat/completions"},
            "loopback or private",
        ),
        ({"HERMES_AGENT_API_KEY": "short"}, "HERMES_AGENT_API_KEY"),
        ({"HERMES_AGENT_MODEL": "default"}, "HERMES_AGENT_MODEL"),
        ({"HERMES_EXECUTION_TIMEOUT_SECONDS": "59"}, "HERMES_EXECUTION_TIMEOUT_SECONDS"),
        ({"HERMES_EXECUTION_TIMEOUT_SECONDS": "3601"}, "HERMES_EXECUTION_TIMEOUT_SECONDS"),
        ({"HERMES_EXECUTION_TIMEOUT_SECONDS": "nan"}, "HERMES_EXECUTION_TIMEOUT_SECONDS"),
        ({"HERMES_EXECUTION_TIMEOUT_SECONDS": ""}, "HERMES_EXECUTION_TIMEOUT_SECONDS"),
    ],
)
def test_worker_preflight_rejects_unsafe_runtime_configuration(
    updates: dict[str, str], message: str
) -> None:
    """A worker must exit before Chrome opens when tenant isolation is incomplete."""

    from deploy.healthcheck import validate_worker_environment

    environment = _runtime_environment()
    environment.update(updates)
    with pytest.raises(ValueError, match=message):
        validate_worker_environment(environment)


def test_worker_preflight_accepts_complete_import_configuration() -> None:
    """A complete static-proxy and fingerprint mapping may start the worker."""

    from deploy.healthcheck import validate_worker_environment

    validate_worker_environment(_runtime_environment())


def test_worker_preflight_accepts_managed_inventory_without_purchase_token() -> None:
    from deploy.healthcheck import validate_worker_environment

    environment = _runtime_environment()
    environment["IPROYAL_MODE"] = "managed"
    environment["IPROYAL_ISP_LEASES_JSON"] = ""
    environment.pop("IPROYAL_ACCESS_TOKEN")

    validate_worker_environment(environment)


def test_worker_preflight_accepts_encrypted_database_inventory() -> None:
    from deploy.healthcheck import validate_worker_environment

    environment = _runtime_environment()
    environment["IPROYAL_MODE"] = "inventory"
    environment["IPROYAL_ISP_LEASES_JSON"] = ""
    environment["IPROYAL_GEO_CATALOG_JSON"] = ""
    environment.pop("IPROYAL_ACCESS_TOKEN")

    validate_worker_environment(environment)


def test_browser_services_reap_chrome_children(tmp_path):
    services = _compose_config(tmp_path)["services"]
    for name in ("api", "worker", "login"):
        assert services[name]["init"] is True


@pytest.mark.parametrize("value", [None, "60", "1800", "3600"])
def test_worker_execution_timeout_defaults_and_boundaries(value):
    from deploy.healthcheck import validate_worker_environment

    environment = _runtime_environment()
    if value is None:
        environment.pop("HERMES_EXECUTION_TIMEOUT_SECONDS")
    else:
        environment["HERMES_EXECUTION_TIMEOUT_SECONDS"] = value
    environment["HERMES_AGENT_TIMEOUT_SECONDS"] = "1"
    validate_worker_environment(environment)


def test_login_browser_has_no_public_port_or_durable_disk(tmp_path):
    service = _compose_config(tmp_path)["services"]["login"]
    assert "ports" not in service
    assert "volumes" not in service
    assert service["user"] == "10001:10001"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["tmpfs"] == ["/tmp:size=512m,mode=1777"]
    assert "--no-access-log" in service["command"]
    assert "IPROYAL_ACCESS_TOKEN" not in service["environment"]
