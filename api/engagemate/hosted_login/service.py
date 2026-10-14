"""Bounded in-memory sign-in lifecycle; database reservations own proxy leases."""

from __future__ import annotations

import asyncio
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from engagemate.hosted_login.protocol import WIDTH, HEIGHT, BrowserCleanupError, ConnectError, SignIn


async def settle(operation):
    """Do not abandon a committing thread/resource after repeated disconnects."""
    task = asyncio.ensure_future(operation)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


@dataclass(repr=False)
class Session:
    reservation: object
    username: str
    expires_at: datetime
    deadline: float
    browser: object = None
    state: str = "active"
    code: str | None = None
    account: dict | None = None
    lease: object = None
    timer: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_complete: float = -100.0
    input_times: list[float] = field(default_factory=list)
    terminal_at: float | None = None

    def public(self):
        result = {"id": str(self.reservation.id), "state": self.state,
                  "expires_at": self.expires_at.isoformat(), "width": WIDTH, "height": HEIGHT,
                  "username": self.username}
        if self.account is not None:
            result["account"] = self.account
        if self.code:
            result["code"] = self.code
        return result


class LoginService:
    def __init__(self, repository, open_browser, *, lease_factory=None, max_sessions=2, ttl=600, cleanup_failure=None):
        self.repository, self.open_browser = repository, open_browser
        self.lease_factory = lease_factory or (lambda account_id: nullcontext())
        self.max_sessions, self.ttl = max_sessions, ttl
        self.sessions: dict[UUID, Session] = {}
        self.admission = asyncio.Lock()
        self.cleanup_failure = cleanup_failure
        self.cleanup_failed = False
        self.shutting_down = False

    async def start(self, user_id, username, timezone_name, account_id=None):
        async with self.admission:
            if self.cleanup_failed or self.shutting_down:
                raise ConnectError("connection_unavailable")
            now = time.monotonic()
            self.sessions = {sid: s for sid, s in self.sessions.items()
                             if s.browser is not None or s.lease is not None
                             or s.terminal_at is None or now - s.terminal_at < 600}
            for session in self.sessions.values():
                if session.reservation.user_id == user_id and session.state == "active":
                    if session.username != username:
                        raise ConnectError("connection_in_progress", 409)
                    return session.public()
            if sum(s.state == "active" or s.browser is not None or s.lease is not None
                   for s in self.sessions.values()) >= self.max_sessions:
                raise ConnectError("connection_capacity", 429)
            # A cancelled HTTP caller must not abandon a DB reservation or Chrome
            # process. The operation is shielded until it can clean up its owner.
            task = asyncio.create_task(self._start(user_id, username, timezone_name, account_id))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                with suppress(Exception):
                    result = await settle(task)
                    await settle(self.cancel(user_id, UUID(result["id"])))
                raise

    async def _start(self, user_id, username, timezone_name, account_id=None):
        reservation = await settle(asyncio.to_thread(
            self.repository.reserve, user_id, username, timezone_name, account_id=account_id,
        ))
        session = Session(reservation, username,
                          datetime.now(timezone.utc) + timedelta(seconds=self.ttl),
                          time.monotonic() + self.ttl)
        self.sessions[reservation.id] = session
        try:
            lease = self.lease_factory(reservation.account_id)
            await settle(asyncio.to_thread(lease.__enter__))
            session.lease = lease
            session.browser = await asyncio.wait_for(self.open_browser(reservation), 60)
            session.timer = asyncio.create_task(self._expire(session))
            return session.public()
        except BrowserCleanupError as error:
            # Keep the half-created browser reachable for one final close. A
            # second failure terminates the dedicated container before reuse.
            session.browser = error.browser
            await self._close(session, code="connection_unavailable")
            raise
        except BaseException:
            await self._close(session, code="connection_unavailable")
            raise

    def owned(self, user_id, session_id):
        session = self.sessions.get(session_id)
        if session is None or session.reservation.user_id != user_id:
            raise ConnectError("connection_not_found", 404)
        return session

    def _active(self, session):
        if time.monotonic() >= session.deadline:
            raise ConnectError("session_expired", 410)
        if session.state != "active" or session.browser is None:
            raise ConnectError(session.code or "connection_closed", 410)

    async def status(self, user_id, session_id):
        if session_id not in self.sessions:
            completed = await asyncio.to_thread(self.repository.completed, session_id, user_id)
            if completed is not None:
                return completed
        session = self.owned(user_id, session_id)
        async with session.lock:
            if session.state == "active" and time.monotonic() >= session.deadline:
                await self._close(session, code="session_expired")
            return session.public()

    async def step(self, user_id, session_id):
        session = self.owned(user_id, session_id)
        async with session.lock:
            self._active(session)
            try:
                return await asyncio.wait_for(session.browser.step(), 8)
            except ConnectError:
                raise
            except Exception:
                raise ConnectError("connection_unavailable") from None

    async def input(self, user_id, session_id, command):
        session = self.owned(user_id, session_id)
        async with session.lock:
            self._active(session)
            if isinstance(command, SignIn) and command.username != session.username:
                raise ConnectError("identity_mismatch", 422)
            now = time.monotonic()
            session.input_times = [t for t in session.input_times if now - t < 1]
            if len(session.input_times) >= 40:
                raise ConnectError("slow_down", 429)
            session.input_times.append(now)
            task = asyncio.create_task(asyncio.wait_for(session.browser.input(command), 10))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A disconnected web request must not abandon a Playwright
                # sign-in half way through. Finish the browser operation before
                # propagating cancellation; keep the session active if it did
                # finish, and close it if the operation failed.
                try:
                    await settle(task)
                except ConnectError:
                    pass
                except BaseException:
                    with suppress(Exception):
                        await settle(self._close(session, code="connection_lost"))
                raise
            except ConnectError:
                raise
            except Exception:
                # A failed click or slow Instagram response is retryable. Keep
                # the temporary browser alive so Check again can recover the
                # current step instead of turning a transient input failure
                # into a dead reconnect session.
                raise ConnectError("connection_unavailable") from None

    async def complete(self, user_id, session_id):
        if session_id not in self.sessions:
            completed = await self.status(user_id, session_id)
            if completed["state"] == "connected":
                return completed
        session = self.owned(user_id, session_id)
        async with session.lock:
            if session.state == "connected":
                return session.public()
            self._active(session)
            if time.monotonic() - session.last_complete < 5:
                raise ConnectError("slow_down", 429)
            session.last_complete = time.monotonic()
            capture = None
            try:
                capture = await asyncio.wait_for(session.browser.capture(session.username), 15)
                self._active(session)
                # Credentials are already captured in memory. End all network
                # activity before a potentially slow database commit; keep the
                # reservation and browser lease until its outcome is known.
                if not await settle(self._close_browser(session)):
                    raise ConnectError("connection_unavailable")
                # Don't cancel a committing transaction on client disconnect. If
                # its outcome is unknown, do not capture or bind another session.
                task = asyncio.create_task(asyncio.to_thread(
                    self.repository.complete, session_id, user_id, capture))
                result = await settle(task)
                session.account = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                session.state = "connected"
                await self._close(session)
                return session.public()
            except ConnectError as error:
                if error.code == "instagram_rate_limited":
                    await self._close(session, code=error.code)
                raise
            except BaseException:
                await self._close(session, code="connection_unavailable")
                raise
            finally:
                if capture is not None:
                    capture.cookies.clear()

    async def cancel(self, user_id, session_id):
        session = self.owned(user_id, session_id)
        async with session.lock:
            await self._close(session, code="connection_cancelled" if session.state != "connected" else None)

    async def _expire(self, session):
        try:
            await asyncio.sleep(max(0, session.deadline - time.monotonic()))
            async with session.lock:
                await self._close(session, code="session_expired")
        except asyncio.CancelledError:
            pass

    async def _close(self, session, code=None):
        await settle(self._close_resources(session, code))

    async def _close_resources(self, session, code=None):
        if session.timer and session.timer is not asyncio.current_task():
            session.timer.cancel()
        if code and session.state != "connected":
            session.state, session.code = "error", code
        session.terminal_at = time.monotonic()
        # Never release a proxy for reuse while its browser can still issue
        # requests. On close failure retain the reservation until its DB TTL.
        closed = await self._close_browser(session)
        if closed:
            if session.lease:
                try:
                    await settle(asyncio.to_thread(session.lease.__exit__, None, None, None))
                except Exception:
                    # Chrome is confirmed closed. Keep a committed result
                    # available to the caller instead of killing the service
                    # before its success response. Refuse new admissions.
                    self.cleanup_failed = True
                    return
                session.lease = None
            with suppress(Exception):
                await settle(asyncio.to_thread(self.repository.release, session.reservation.id, session.reservation.user_id))

    async def _close_browser(self, session):
        closed = True
        if session.browser:
            try:
                await asyncio.wait_for(session.browser.close(), 10)
            except BaseException:
                closed = False
            if closed:
                session.browser = None
            else:
                self.cleanup_failed = True
                if self.cleanup_failure:
                    self.cleanup_failure()
        return closed

    async def close(self):
        # Start owns admission until Chrome is assigned. Shutdown must not
        # release that reservation while the browser is still being created.
        async with self.admission:
            self.shutting_down = True
            for session in list(self.sessions.values()):
                async with session.lock:
                    await self._close(session, code="connection_closed")
