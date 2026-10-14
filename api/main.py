"""EngageMate control API application."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from engagemate.config import Settings
from engagemate.control.routes.accounts import router as accounts_router
from engagemate.control.routes.jobs import account_jobs_router, jobs_router
from engagemate.control.routes.mcp_oauth import router as mcp_oauth_router
from engagemate.control.routes.onboarding import router as onboarding_router
from engagemate.control.routes.profiles import router as profiles_router
from engagemate.db.pool import create_pool
from engagemate.mcp.control_server import build_control_mcp
from engagemate.security.session_cipher import SessionCipher
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)


class _McpDispatch:
    """Forward unmatched public routes to the MCP app created at startup."""

    def __init__(self, parent: FastAPI) -> None:
        self.parent = parent

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        mcp_app = getattr(self.parent.state, "control_mcp_app", None)
        if mcp_app is None:
            response = JSONResponse(
                status_code=503,
                content={"detail": "MCP service unavailable"},
            )
            await response(scope, receive, send)
            return
        await mcp_app(scope, receive, send)


def create_app(
    *,
    settings: Settings | None = None,
    pool: ConnectionPool | None = None,
) -> FastAPI:
    """Build the API with injectable settings and database resources."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings or Settings()
        SessionCipher.validate_key(
            application.state.settings.session_encryption_key.get_secret_value()
        )
        application.state.db_pool = pool or create_pool(
            application.state.settings.database_url
        )
        application.state.owns_db_pool = pool is None
        try:
            if not application.state.settings.mcp_configured:
                # No MCP URLs means no public OAuth issuer to trust. The
                # dispatcher answers 503 for MCP routes until they are set.
                logger.warning(
                    "Control MCP disabled: set MCP_ISSUER, MCP_RESOURCE_URL, "
                    "MCP_CONSENT_URL and ENGAGEMATE_WEB_ORIGIN to enable it"
                )
                application.state.control_mcp = None
                application.state.control_mcp_app = None
                yield
                return
            control_mcp = build_control_mcp(
                application.state.settings,
                application.state.db_pool,
            )
            control_mcp_app = control_mcp.streamable_http_app(
                streamable_http_path="/mcp",
                host="0.0.0.0",
            )
            application.state.control_mcp = control_mcp
            application.state.control_mcp_app = control_mcp_app
            async with control_mcp_app.router.lifespan_context(control_mcp_app):
                yield
        finally:
            if application.state.owns_db_pool:
                application.state.db_pool.close()

    application = FastAPI(
        title="EngageMate Control API",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            os.getenv("FRONTEND_URL", "http://localhost:3000"),
            "http://localhost:3001",
            "http://localhost:3002",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request, error: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default handler echoes invalid input. Session payloads contain
        # cookies, so return only structural validation details.
        safe_errors = [
            {
                "type": item["type"],
                "loc": item["loc"],
                "msg": item["msg"],
            }
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": safe_errors})

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "1.0.0"}

    @application.get("/v1/mcp/health")
    def mcp_health() -> dict[str, str | bool]:
        with application.state.db_pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "oauth": getattr(application.state, "control_mcp_app", None) is not None,
        }

    application.include_router(accounts_router, prefix="/v1")
    application.include_router(profiles_router, prefix="/v1")


    application.include_router(onboarding_router, prefix="/v1")
    application.include_router(account_jobs_router, prefix="/v1")
    application.include_router(jobs_router, prefix="/v1")
    application.include_router(mcp_oauth_router, prefix="/v1")
    application.mount("/", _McpDispatch(application), name="control-mcp")
    return application


app = create_app()
