"""Typed runtime configuration for EngageMate services."""

from ipaddress import ip_address
from pathlib import Path
import re
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from engagemate.web_origin import normalize_origin


class Settings(BaseSettings):
    """Environment-backed settings shared by the API, scheduler, and worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    database_url: str = Field(validation_alias="DATABASE_URL", repr=False)
    session_encryption_key: SecretStr = Field(validation_alias="SESSION_ENCRYPTION_KEY")
    internal_api_key: SecretStr = Field(validation_alias="INTERNAL_API_KEY")
    hosted_login_enabled: bool = Field(default=False, validation_alias="HOSTED_LOGIN_ENABLED")
    hosted_login_browser_executable: str | None = Field(
        default=None, validation_alias="HOSTED_LOGIN_BROWSER_EXECUTABLE"
    )
    hosted_login_max_sessions: int = Field(
        default=2, ge=1, le=2, validation_alias="HOSTED_LOGIN_MAX_SESSIONS"
    )
    # Public web origin for links and the MCP setup URL. No default: an unset
    # value disables those features instead of trusting a guessed hostname.
    public_app_url: str | None = Field(
        default=None,
        validation_alias="ENGAGEMATE_WEB_ORIGIN",
    )
    # The customer Control MCP is off until all three URLs are configured.
    mcp_issuer: AnyHttpUrl | None = Field(
        default=None,
        validation_alias="MCP_ISSUER",
    )
    mcp_resource_url: AnyHttpUrl | None = Field(
        default=None,
        validation_alias="MCP_RESOURCE_URL",
    )
    mcp_redirect_hosts: Annotated[tuple[str, ...], NoDecode] = Field(
        default=("localhost", "127.0.0.1", "::1"),
        validation_alias="MCP_REDIRECT_HOSTS",
    )
    mcp_consent_url: AnyHttpUrl | None = Field(
        default=None,
        validation_alias="MCP_CONSENT_URL",
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="ANTHROPIC_API_KEY",
    )
    intelligence_provider: Literal["hermes_agent", "anthropic"] = Field(
        default="hermes_agent",
        validation_alias="INTELLIGENCE_PROVIDER",
    )
    # Pinned to a NON-PUBLIC endpoint, not to one exact string. The Hermes
    # gateway binds the host loopback, which a containerised API cannot reach
    # from its bridge network, so deployments must be able to point at the
    # bridge gateway (or another private address) instead. The validator keeps
    # the original guarantee that intact: prompts never leave the host/private
    # network for some arbitrary public LLM endpoint.
    hermes_agent_url: str = Field(
        default="http://127.0.0.1:8657/v1/chat/completions",
        validation_alias="HERMES_AGENT_URL",
    )
    hermes_agent_api_key: SecretStr | None = Field(
        default=None,
        min_length=32,
        validation_alias="HERMES_AGENT_API_KEY",
    )
    hermes_agent_model: Literal["engagemate-agent"] = Field(
        default="engagemate-agent",
        validation_alias="HERMES_AGENT_MODEL",
    )
    hermes_agent_timeout_seconds: float = Field(
        default=60.0,
        ge=1,
        le=120,
        validation_alias="HERMES_AGENT_TIMEOUT_SECONDS",
    )
    hermes_execution_timeout_seconds: float = Field(
        default=1800.0,
        ge=60,
        le=3600,
        validation_alias="HERMES_EXECUTION_TIMEOUT_SECONDS",
    )
    instagram_execution_mode: Literal["disabled", "canary", "autonomous"] = Field(
        default="disabled",
        validation_alias="INSTAGRAM_EXECUTION_MODE",
    )
    execution_mcp_host: Literal["127.0.0.1"] = Field(
        default="127.0.0.1",
        validation_alias="EXECUTION_MCP_HOST",
    )
    execution_mcp_port: int = Field(
        default=8766,
        ge=1024,
        le=65535,
        validation_alias="EXECUTION_MCP_PORT",
    )
    execution_mcp_key: SecretStr | None = Field(
        default=None,
        min_length=32,
        validation_alias="ENGAGEMATE_EXECUTION_MCP_KEY",
    )

    # Public Instagram discovery is a separate retrieval boundary: Hermes only
    # interprets the returned public data and never receives a retrieval
    # credential. Apify was the original provider and has been fully replaced by
    # HyperCrawl, so APIFY_TOKEN / APIFY_PROFILE_ACTOR / APIFY_SEARCH_ACTOR /
    # APIFY_TIMEOUT_SECONDS are gone. Leaving them set is harmless; nothing
    # reads them.
    hypercrawl_timeout_seconds: float = Field(
        # Was 90s, inherited from the Apify setting this replaces, and too tight
        # for the work it now covers: a SERP search escalates across engines and
        # was MEASURED at 50-52s before a single profile is fetched. 90s left
        # almost no headroom on a call that is expected to be slow, so a busy
        # run would have failed as a timeout rather than a slow success.
        default=240.0,
        ge=10,
        le=600,
        validation_alias="HYPERCRAWL_TIMEOUT_SECONDS",
    )
    # Retrieval runs through the internal HyperCrawl service. Retrieval-only.
    hypercrawl_url: str | None = Field(
        default=None, validation_alias="HYPERCRAWL_URL"
    )
    hypercrawl_rest_token: SecretStr | None = Field(
        default=None, validation_alias="HYPERCRAWL_REST_TOKEN"
    )
    growth_snapshot_batch_size: int = Field(
        default=25,
        ge=1,
        le=100,
        validation_alias="GROWTH_SNAPSHOT_BATCH_SIZE",
    )
    growth_snapshot_concurrency: int = Field(
        default=4,
        ge=1,
        le=25,
        validation_alias="GROWTH_SNAPSHOT_CONCURRENCY",
    )
    resend_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="RESEND_API_KEY",
        repr=False,
    )
    composio_api_key: SecretStr | None = Field(default=None, validation_alias="COMPOSIO_API_KEY", repr=False)
    composio_email_entity_id: str = Field(default="", validation_alias="COMPOSIO_EMAIL_ENTITY_ID")
    composio_email_account_id: str | None = Field(default=None, validation_alias="COMPOSIO_EMAIL_ACCOUNT_ID")
    action_email_from: str | None = Field(default=None, validation_alias="ACTION_EMAIL_FROM")
    weekly_report_from: str | None = Field(
        default=None,
        validation_alias="WEEKLY_REPORT_FROM",
        repr=False,
    )
    weekly_report_batch_size: int = Field(
        default=25,
        ge=1,
        le=100,
        validation_alias="WEEKLY_REPORT_BATCH_SIZE",
    )
    weekly_report_canary_user_id: UUID | None = Field(
        default=None,
        validation_alias="WEEKLY_REPORT_CANARY_USER_ID",
    )
    weekly_report_canary_recipient: str | None = Field(
        default=None,
        validation_alias="WEEKLY_REPORT_CANARY_RECIPIENT",
        repr=False,
    )

    @field_validator(
        "weekly_report_canary_user_id",
        "weekly_report_canary_recipient",
        mode="before",
    )
    @classmethod
    def normalize_blank_canary_values(cls, value: object) -> object:
        """Treat blank optional canary environment values as unset."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    # Two models, and neither is a tier: triage answers the worth-replying
    # gate, and every reply a target sees is written by the strongest model.
    triage_model: Literal["claude-haiku-4-5"] = Field(
        default="claude-haiku-4-5",
        validation_alias="ANTHROPIC_TRIAGE_MODEL",
    )
    decision_model: Literal["claude-opus-4-8"] = Field(
        default="claude-opus-4-8",
        validation_alias="ANTHROPIC_DECISION_MODEL",
    )
    job_lease_seconds: int = Field(
        default=120,
        ge=30,
        validation_alias="JOB_LEASE_SECONDS",
    )
    heartbeat_interval_seconds: int = Field(
        default=30,
        ge=5,
        validation_alias="HEARTBEAT_INTERVAL_SECONDS",
    )
    fingerprint_seed: SecretStr | None = Field(
        default=None,
        validation_alias="FINGERPRINT_SEED",
    )
    fingerprint_geo_map_json: SecretStr | None = Field(
        default=None,
        validation_alias="FINGERPRINT_GEO_MAP_JSON",
    )

    # Import mode reads operator-provisioned leases. Managed mode lets the
    # scheduler purchase bounded shortfalls and store them as encrypted inventory.
    iproyal_mode: Literal["disabled", "import", "inventory", "managed"] = Field(
        default="disabled",
        validation_alias="IPROYAL_MODE",
    )
    iproyal_isp_leases_json: SecretStr | None = Field(
        default=None,
        validation_alias="IPROYAL_ISP_LEASES_JSON",
    )
    iproyal_access_token: SecretStr | None = Field(
        default=None,
        min_length=16,
        validation_alias="IPROYAL_ACCESS_TOKEN",
    )
    iproyal_geo_catalog_json: SecretStr | None = Field(
        default=None,
        validation_alias="IPROYAL_GEO_CATALOG_JSON",
    )
    # No spare capacity is purchased by default.
    iproyal_spare_per_geo: int = Field(
        default=0,
        ge=0,
        le=20,
        validation_alias="IPROYAL_SPARE_PER_GEO",
    )
    # A lease bills every month, so the ceiling on trial-driven purchases is a
    # count of concurrent leases rather than a monthly spend: bounding the
    # recurring commitment is what matters, not one month's outlay.
    iproyal_max_trial_leases: int = Field(
        default=5,
        ge=0,
        le=50,
        validation_alias="IPROYAL_MAX_TRIAL_LEASES",
    )

    # Geography consensus. Without anchors the health check keeps its original
    # single-oracle behaviour, so these are additive rather than required.
    proxy_geo_anchors_json: SecretStr | None = Field(
        default=None,
        validation_alias="PROXY_GEO_ANCHORS_JSON",
    )
    # 25 miles, calibrated against the 2026-08-17 readings: it keeps a lease
    # nine miles from Hempstead and rejects one geolocating to Virginia when
    # it was sold as Washington DC.
    proxy_geo_radius_miles: float = Field(
        default=25.0,
        gt=0,
        le=500.0,
        validation_alias="PROXY_GEO_RADIUS_MILES",
    )
    # Some providers answer an unknown address with a country centroid rather
    # than an error. Anything vaguer than this is discarded before voting.
    proxy_geo_max_accuracy_km: float = Field(
        default=100.0,
        gt=0,
        le=5_000.0,
        validation_alias="PROXY_GEO_MAX_ACCURACY_KM",
    )
    proxy_geo_providers: str = Field(
        default="",
        validation_alias="PROXY_GEO_PROVIDERS",
    )
    iproyal_max_order_quantity: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias="IPROYAL_MAX_ORDER_QUANTITY",
    )
    # Default bounded purchase ceiling for explicitly enabled managed mode.
    iproyal_max_order_cost_cents: int = Field(
        default=3_000,
        ge=1,
        le=100_000,
        validation_alias="IPROYAL_MAX_ORDER_COST_CENTS",
    )
    # Also $30, and now genuinely $30: `_get_snapshot` used to scope the spend
    # total to the geography being planned, which made this a per-city
    # allowance worth seven times its face value. It counts orders placed this
    # calendar month, not the recurring bill those leases go on to generate --
    # spending the ceiling every month still compounds the monthly commitment,
    # so this bounds the rate of new purchases rather than total exposure.
    iproyal_max_monthly_cost_cents: int = Field(
        default=3_000,
        ge=1,
        le=1_000_000,
        validation_alias="IPROYAL_MAX_MONTHLY_COST_CENTS",
    )
    iproyal_api_timeout_seconds: float = Field(
        default=15.0,
        ge=1,
        le=30,
        validation_alias="IPROYAL_API_TIMEOUT_SECONDS",
    )
    proxy_health_url: str | None = Field(
        default=None,
        validation_alias="PROXY_HEALTH_URL",
    )
    proxy_connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=15,
        validation_alias="PROXY_CONNECT_TIMEOUT_SECONDS",
    )
    proxy_read_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=30,
        validation_alias="PROXY_READ_TIMEOUT_SECONDS",
    )

    # Worker failure diagnostics are opt-in. Artifacts derive from a logged-in
    # Instagram session, so the default is to keep collecting nothing.
    artifact_dir: str | None = Field(
        default=None,
        validation_alias="ARTIFACT_DIR",
    )

    @field_validator("public_app_url", mode="before")
    @classmethod
    def normalize_public_app_url(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_origin(str(value))

    @property
    def mcp_configured(self) -> bool:
        return None not in (self.mcp_issuer, self.mcp_resource_url, self.mcp_consent_url)

    @field_validator("mcp_issuer", "mcp_resource_url", "mcp_consent_url", mode="before")
    @classmethod
    def blank_mcp_url_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("mcp_redirect_hosts", mode="before")
    @classmethod
    def parse_mcp_redirect_hosts(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            hosts = tuple(part.strip().lower() for part in value.split(","))
        elif isinstance(value, (list, tuple)):
            hosts = tuple(str(part).strip().lower() for part in value)
        else:
            raise ValueError("MCP_REDIRECT_HOSTS must be a comma-separated host list")
        if not hosts or any(not host for host in hosts):
            raise ValueError("MCP_REDIRECT_HOSTS must include at least one host")
        for host in hosts:
            if host == "::1":
                continue
            if not re.fullmatch(r"[a-z0-9.-]+", host):
                raise ValueError("MCP_REDIRECT_HOSTS entries must be bare hosts")
        return tuple(dict.fromkeys(hosts))

    @model_validator(mode="after")
    def validate_job_lease_heartbeat(self) -> "Settings":
        """Keep the worker heartbeat comfortably inside the claimed job lease."""

        if self.heartbeat_interval_seconds * 2 > self.job_lease_seconds:
            raise ValueError(
                "HEARTBEAT_INTERVAL_SECONDS must be no more than half of "
                "JOB_LEASE_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def validate_mcp_urls(self) -> "Settings":
        urls = {
            "MCP_ISSUER": self.mcp_issuer,
            "MCP_RESOURCE_URL": self.mcp_resource_url,
            "MCP_CONSENT_URL": self.mcp_consent_url,
        }
        missing = [name for name, value in urls.items() if value is None]
        if len(missing) == len(urls):
            return self
        if missing:
            raise ValueError(
                "Set MCP_ISSUER, MCP_RESOURCE_URL, and MCP_CONSENT_URL together; "
                f"missing {', '.join(missing)}"
            )
        parsed = {name: urlparse(str(value)) for name, value in urls.items()}
        for name, value in parsed.items():
            host = value.hostname
            if not host:
                raise ValueError(f"{name} must include a host")
            if value.username or value.password or value.query or value.fragment:
                raise ValueError(f"{name} must not include credentials, a query, or a fragment")
            if value.scheme != "https" and not (
                value.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError(f"{name} must use HTTPS outside loopback")
        issuer = parsed["MCP_ISSUER"]
        resource = parsed["MCP_RESOURCE_URL"]
        if issuer.path not in {"", "/"}:
            raise ValueError("MCP_ISSUER must not include a path")
        if resource.path != "/mcp":
            raise ValueError("MCP_RESOURCE_URL must use the /mcp path")
        if (issuer.hostname, issuer.port) != (resource.hostname, resource.port):
            raise ValueError("MCP_ISSUER and MCP_RESOURCE_URL must share a host")
        return self

    @field_validator("hermes_agent_url")
    @classmethod
    def validate_hermes_agent_url_is_private(cls, value: str) -> str:
        """Reject any Hermes endpoint that is not loopback or private."""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("hermes_agent_url must be an http(s) URL")
        host = parsed.hostname
        if not host:
            raise ValueError("hermes_agent_url must include a host")
        try:
            address = ip_address(host)
        except ValueError as exc:
            # A hostname could resolve anywhere, including off-box. Require a
            # literal address so the private-only guarantee is checkable here.
            raise ValueError(
                "hermes_agent_url host must be a literal IP address "
                "(loopback or private), not a hostname"
            ) from exc
        if not (address.is_loopback or address.is_private):
            raise ValueError(
                "hermes_agent_url must point at a loopback or private address; "
                f"{host} is publicly routable"
            )
        return value

    @model_validator(mode="after")
    def validate_artifact_dir(self) -> "Settings":
        """Treat a blank directory as off and require an absolute path when on."""

        if self.artifact_dir is None:
            return self
        configured = self.artifact_dir.strip()
        if not configured:
            self.artifact_dir = None
            return self
        if not Path(configured).is_absolute():
            raise ValueError("ARTIFACT_DIR must be an absolute path")
        self.artifact_dir = configured
        return self

    @model_validator(mode="after")
    def validate_iproyal_configuration(self) -> "Settings":
        """Require complete, secret-safe settings for the selected mode."""

        if self.iproyal_mode == "disabled":
            return self
        if self.iproyal_mode == "import":
            inventory = self.iproyal_isp_leases_json
            if inventory is None or not inventory.get_secret_value().strip():
                raise ValueError(
                    "IPRoyal import mode requires an ISP lease inventory and HTTPS "
                    "health URL"
                )
            if not self.proxy_health_url or not self.proxy_health_url.startswith(
                "https://"
            ):
                raise ValueError("IPRoyal mode requires an HTTPS health URL")
            return self
        if self.iproyal_mode == "inventory":
            if not self.proxy_health_url or not self.proxy_health_url.startswith(
                "https://"
            ):
                raise ValueError("IPRoyal mode requires an HTTPS health URL")
            return self
        if not self.proxy_health_url or not self.proxy_health_url.startswith(
            "https://"
        ):
            raise ValueError("IPRoyal mode requires an HTTPS health URL")
        catalog = self.iproyal_geo_catalog_json
        if catalog is None or not catalog.get_secret_value().strip():
            raise ValueError("IPRoyal managed mode requires a valid geo catalog")
        try:
            from engagemate.proxy.capacity import parse_geo_catalog

            parse_geo_catalog(catalog.get_secret_value())
        except ValueError:
            raise ValueError(
                "IPRoyal managed mode requires a valid geo catalog"
            ) from None
        return self
