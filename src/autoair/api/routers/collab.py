from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from autoair.api.deps import ServicesDep
from autoair.collab import ROOM_RE, available, issue_ticket

router = APIRouter(prefix="/collab", tags=["collab"])


class CollabTicket(BaseModel):
    ticket: str
    expires_in: int


@router.post("/{dag_id}/ticket", operation_id="collabTicket")
async def collab_ticket(dag_id: str, services: ServicesDep) -> CollabTicket:
    """Short-lived ticket to join the DAG's live editing room (needs DAG edit permission)."""
    if not available():
        raise HTTPException(status_code=404, detail="collaboration is disabled")
    if not ROOM_RE.fullmatch(f"collab/{dag_id}"):
        raise HTTPException(status_code=400, detail="invalid dag_id")
    ttl = services.settings.collab_ticket_ttl
    return CollabTicket(
        ticket=issue_ticket(services.collab_secret, dag_id, ttl=ttl), expires_in=ttl
    )
