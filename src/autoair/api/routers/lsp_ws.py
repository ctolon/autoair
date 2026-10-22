"""Language servers over WebSockets (``features.ide_language_server``).

The browser asks the authenticated HTTP API for a short-lived ticket
(``POST /api/lsp/session``, ``builder:use``) and opens ``/api/lsp/ws/{language}?ticket=…``;
the WebSocket checks the ticket (an HMAC, like the collaboration rooms), so no long-lived
credential appears in a URL and every Airflow auth manager works. A ticket names the user it
was issued to (the session runs as that user) and opens one socket: its nonce is claimed in
the shared :class:`~autoair.collab.TicketLedger` (collection ``lsp_tickets``), so a leaked
ticket cannot be replayed on any replica within its TTL. See :mod:`autoair.lsp.proxy` for the
session itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Literal

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.app import Services
from autoair.api.deps import ServicesDep
from autoair.api.features import requires_feature
from autoair.collab import TicketLedger, issue_ticket, ticket_ledger, verify_ticket
from autoair.iam.deps import PrincipalDep
from autoair.lsp.proxy import LspError, LspSessionManager, server_status

FEATURE = "ide_language_server"
TICKET_TTL = 60  # seconds; only checked when the socket opens

log = logging.getLogger(__name__)

router = APIRouter(prefix="/lsp", tags=["lsp"], dependencies=[requires_feature(FEATURE)])
ws_router = APIRouter(prefix="/lsp")


class LanguageServerStatus(BaseModel):
    server: str = Field(description="basedpyright, pylsp or bash-language-server.")
    available: bool = Field(description="False when the server binary is not installed.")


class LanguageServers(BaseModel):
    python: LanguageServerStatus
    bash: LanguageServerStatus


class LspSessionRequest(BaseModel):
    language: Literal["python", "bash"]


class LspSessionTicket(BaseModel):
    ticket: str
    expires_in: int
    path: str = Field(description="WebSocket path below the API root, e.g. /lsp/ws/python.")


def _status(services: Services) -> LanguageServers:
    return LanguageServers.model_validate(server_status(services.settings.lsp))


@router.get("/servers", operation_id="lspServers")
async def lsp_servers(services: ServicesDep) -> LanguageServers:
    """Which language servers are installed (the editor falls back to the built-in
    completions for a missing one)."""
    return await run_in_threadpool(_status, services)


@router.post("/session", operation_id="lspSession")
async def lsp_session(
    body: LspSessionRequest, services: ServicesDep, principal: PrincipalDep
) -> LspSessionTicket:
    """Ticket to open a language-server WebSocket for ``language``."""
    ticket = issue_ticket(
        services.collab_secret, f"lsp:{body.language}", principal.user, ttl=TICKET_TTL
    )
    return LspSessionTicket(ticket=ticket, expires_in=TICKET_TTL, path=f"/lsp/ws/{body.language}")


def _manager(websocket: WebSocket, services: Services) -> LspSessionManager:
    state = websocket.app.state
    manager = getattr(state, "lsp_manager", None)
    if not isinstance(manager, LspSessionManager) or manager.settings is not services.settings.lsp:
        manager = LspSessionManager(services.settings.lsp)
        state.lsp_manager = manager
    return manager


def _ledger(websocket: WebSocket, services: Services) -> TicketLedger:
    state = websocket.app.state
    cached = getattr(state, "lsp_tickets", None)
    if isinstance(cached, tuple) and cached[0] is services:
        ledger: TicketLedger = cached[1]
        return ledger
    ledger = ticket_ledger(services.settings, services.database, collection="lsp_tickets")
    state.lsp_tickets = (services, ledger)
    return ledger


async def _claimed(websocket: WebSocket, services: Services, claims: dict[str, object]) -> bool:
    """``True`` the first time this ticket opens a socket, on any replica."""
    try:
        return await asyncio.to_thread(_ledger(websocket, services).claim, dict(claims))
    except Exception:  # the ledger is unreachable: fail closed
        log.warning("lsp: cannot record the ticket; refusing", exc_info=True)
        return False


async def _deny(websocket: WebSocket, status: int, detail: str) -> None:
    try:
        await websocket.send_denial_response(Response(detail, status_code=status))
    except RuntimeError:  # the server lacks the denial-response extension
        await websocket.close(code=1008, reason=detail)


@ws_router.websocket("/ws/{language}")
async def lsp_socket(websocket: WebSocket, language: str) -> None:
    services: Services = websocket.app.state.services
    if not services.settings.feature(FEATURE):
        await _deny(websocket, 404, f"the {FEATURE} feature is disabled")
        return
    claims = verify_ticket(
        services.collab_secret, websocket.query_params.get("ticket"), f"lsp:{language}"
    )
    if claims is None:
        await _deny(websocket, 403, "missing, invalid or expired ticket (POST /api/lsp/session)")
        return
    if not await _claimed(websocket, services, claims):
        await _deny(websocket, 403, "ticket already used (POST /api/lsp/session for a new one)")
        return
    manager = _manager(websocket, services)
    await websocket.accept()

    async def receive() -> str | None:
        try:
            return await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return None

    async def send(text: str) -> None:
        await websocket.send_text(text)

    try:
        await manager.run(language, str(claims.get("u") or "anonymous"), receive, send)
    except LspError as exc:
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=exc.code, reason=str(exc)[:120])
        return
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=1000)


__all__ = ["router", "ws_router"]
