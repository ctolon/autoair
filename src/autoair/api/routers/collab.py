from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from autoair.api.deps import ServicesDep
from autoair.collab import ROOM_RE, available, issue_ticket
from autoair.iam.deps import PrincipalDep

router = APIRouter(prefix="/collab", tags=["collab"])


class CollabTicket(BaseModel):
    ticket: str
    expires_in: int


@router.post("/{dag_id}/ticket", operation_id="collabTicket")
async def collab_ticket(
    dag_id: str, services: ServicesDep, principal: PrincipalDep
) -> CollabTicket:
    """Short-lived, single-use ticket for one connection to the DAG's live editing room,
    bound to the caller and the room (needs DAG edit permission)."""
    if not available():
        raise HTTPException(status_code=404, detail="collaboration is disabled")
    if not ROOM_RE.fullmatch(f"collab/{dag_id}"):
        raise HTTPException(status_code=400, detail="invalid dag_id")
    ttl = services.settings.collab_ticket_ttl
    return CollabTicket(
        ticket=issue_ticket(services.collab_secret, dag_id, user=principal.user, ttl=ttl),
        expires_in=ttl,
    )
