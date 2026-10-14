"""Private service; public callers reach it only through the Clerk web tier."""

import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from engagemate.browser.lease import BrowserBusy, browser_lease
from engagemate.config import Settings
from engagemate.control.dependencies import current_user_id
from engagemate.db.pool import create_pool
from engagemate.hosted_login.browser import LoginBrowser
from engagemate.hosted_login.protocol import Command, ConnectError, StartRequest
from engagemate.hosted_login.service import LoginService
from engagemate.security.session_cipher import SessionCipher


class PrivateBodyLimit:
    """Bound even chunked input before JSON parsing; never inspect or log it."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages, size = [], 0
        if scope["method"] in {"POST", "PUT", "PATCH"}:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > 8192:
                    response = JSONResponse({"code": "invalid_input"}, status_code=413)
                    return await response(scope, receive, send)
                messages.append(message)
                if not message.get("more_body"):
                    break

        async def bounded_receive():
            return messages.pop(0) if messages else await receive()

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store, private"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)
        await self.app(scope, bounded_receive, private_send)


def create_app(*, settings=None, service=None):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings()
        app.state.settings = config
        app.state.login_service = service
        pool = playwright = None
        try:
            if service is None and config.hosted_login_enabled:
                from playwright.async_api import async_playwright
                from engagemate.db.hosted_login import HostedLoginRepository
                SessionCipher.validate_key(config.session_encryption_key.get_secret_value())
                pool = create_pool(config.database_url, max_size=6)
                playwright = await async_playwright().start()
                from deploy.healthcheck import probe_hosted_login_schema
                probe_hosted_login_schema(
                    {"HOSTED_LOGIN_ENABLED": "true", "DATABASE_URL": config.database_url},
                    connect=lambda *args, **kwargs: pool.connection(),
                )
                repository = HostedLoginRepository(pool, config)
                app.state.login_service = LoginService(
                    repository, lambda reservation: LoginBrowser.open(playwright, reservation, config),
                    lease_factory=lambda account_id: browser_lease(pool, account_id, idle_timeout_seconds=720),
                    max_sessions=config.hosted_login_max_sessions,
                    # This dedicated container must die with all child Chrome
                    # processes if a close cannot be confirmed. DB reservations
                    # stay held until expiry; never hand out a live browser lease.
                    cleanup_failure=lambda: os._exit(1),
                )
            yield
        finally:
            if app.state.login_service:
                await app.state.login_service.close()
            if playwright:
                await playwright.stop()
            if pool:
                pool.close()

    app = FastAPI(title="EngageMate private sign-in", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PrivateBodyLimit)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(_request, _error):
        return JSONResponse({"code": "invalid_input"}, status_code=422)

    @app.exception_handler(ConnectError)
    async def connection_error(_request, error):
        return JSONResponse({"code": error.code}, status_code=error.status)

    @app.get("/health")
    async def health():
        return {"status": "ok", "enabled": app.state.settings.hosted_login_enabled}

    def manager(request: Request):
        if not app.state.settings.hosted_login_enabled or not app.state.login_service:
            raise ConnectError("connection_unavailable", 503)
        return app.state.login_service

    async def safely(operation):
        try:
            return await operation
        except ConnectError:
            raise
        except BrowserBusy:
            return JSONResponse({"code": "account_browser_busy"}, status_code=409)
        except Exception as error:
            # DB and browser exceptions can contain credentials. Only explicitly
            # recognized domain codes cross this boundary; no stack logging.
            safe_codes = {
                "capacity_waitlist_required": 409, "capacity_unavailable": 409,
                "geography_unavailable": 422, "session_unavailable": 410,
                "account_unavailable": 404, "invalid_username": 422, "account_limit_reached": 402,
                "account_limit": 402, "proxy_unavailable": 503,
                "unsupported_geography": 422, "location_unavailable": 422,
                "identity_mismatch": 422, "session_expired": 410,
                "reservation_expired": 410, "connection_in_progress": 409,
                "proxy_health_failed": 503,
            }
            code = getattr(error, "code", None)
            if not isinstance(code, str) or code not in safe_codes:
                code = "connection_unavailable"
            return JSONResponse({"code": code}, status_code=safe_codes.get(code, 503))

    User = Annotated[UUID, Depends(current_user_id)]
    Service = Annotated[LoginService, Depends(manager)]

    @app.post("/v1/connect")
    async def start(body: StartRequest, user_id: User, svc: Service):
        return await safely(svc.start(user_id, body.username, body.timezone, body.account_id))

    @app.get("/v1/connect/{session_id}")
    async def status(session_id: UUID, user_id: User, svc: Service):
        return await safely(svc.status(user_id, session_id))

    @app.get("/v1/connect/{session_id}/step")
    async def step(session_id: UUID, user_id: User, svc: Service):
        return await safely(svc.step(user_id, session_id))

    @app.post("/v1/connect/{session_id}/input", status_code=204)
    async def input_event(body: Command, session_id: UUID, user_id: User, svc: Service):
        result = await safely(svc.input(user_id, session_id, body))
        return result if isinstance(result, Response) else Response(status_code=204)

    @app.post("/v1/connect/{session_id}/complete")
    async def complete(session_id: UUID, user_id: User, svc: Service):
        return await safely(svc.complete(user_id, session_id))

    @app.delete("/v1/connect/{session_id}", status_code=204)
    async def cancel(session_id: UUID, user_id: User, svc: Service):
        result = await safely(svc.cancel(user_id, session_id))
        return result if isinstance(result, Response) else Response(status_code=204)

    return app


app = create_app()
