# ruff: noqa: B008 -- FastAPI registers dependency defaults.
"""Owner-scoped equivalents of console reads. Never use the console admin bypass."""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlmodel import Session

from app.db import get_session
from app.db.models import HumanHandoffRequest, MemoryRecord
from app.public_api.auth import PublicPrincipal, require_scopes
from app.public_api.errors import PublicAPIError
from app.public_api.sessions import ensure_public_agent, owned_public_session

router = APIRouter(prefix="/agents/{agent_id}", tags=["private-content"])


@router.get("/sessions/{session_id}/detail")
def detail(
    agent_id: str,
    session_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sessions:read")),
    db: Session = Depends(get_session),
):
    row, _ = owned_public_session(db, principal, agent_id, session_id)
    from app.api.sessions import _session_detail_payload

    return _session_detail_payload(db, principal.tenant_id, row)


@router.post("/sessions/{session_id}/reset")
def reset(
    agent_id: str,
    session_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sessions:write")),
    db: Session = Depends(get_session),
):
    owned_public_session(db, principal, agent_id, session_id)
    from app.api.sessions import reset_session

    return reset_session(session_id, principal.tenant_id, principal.actor_user, db)


@router.get("/sessions/{session_id}/artifacts/{task_frame_id}")
def artifact(
    agent_id: str,
    session_id: str,
    task_frame_id: str,
    path: str = Query(...),
    principal: PublicPrincipal = Depends(require_scopes("sessions:read")),
    db: Session = Depends(get_session),
):
    owned_public_session(db, principal, agent_id, session_id)
    from app.api.chat import download_harness_artifact

    return download_harness_artifact(
        session_id, task_frame_id, principal.tenant_id, path, principal.actor_user, db
    )


@router.post("/attachments")
async def upload(
    agent_id: str,
    files: list[UploadFile] = File(...),
    principal: PublicPrincipal = Depends(require_scopes("sessions:write")),
    db: Session = Depends(get_session),
):
    ensure_public_agent(db, principal, agent_id)
    from app.api.chat import upload_chat_attachments

    return await upload_chat_attachments(principal.tenant_id, files, principal.actor_user, db)


@router.get("/memories")
def memories(
    agent_id: str,
    limit: int = Query(500, ge=1, le=500),
    principal: PublicPrincipal = Depends(require_scopes("memories:read")),
    db: Session = Depends(get_session),
):
    ensure_public_agent(db, principal, agent_id)
    from app.memory.service import MemoryService, memory_read

    rows = MemoryService(db)._list_user_memories(
        principal.tenant_id, principal.actor_user.id, agent_id=agent_id, limit=limit
    )
    return [memory_read(row) for row in rows]


@router.delete("/memories/{memory_id}")
def delete_memory(
    agent_id: str,
    memory_id: str,
    principal: PublicPrincipal = Depends(require_scopes("memories:write")),
    db: Session = Depends(get_session),
):
    ensure_public_agent(db, principal, agent_id)
    from app.memory.service import MemoryService

    row = db.get(MemoryRecord, memory_id)
    if (
        row is None
        or row.tenant_id != principal.tenant_id
        or row.user_id != principal.actor_user.id
        or not MemoryService(db)._memory_matches_agent(row, agent_id)
    ):
        raise PublicAPIError(404, "MEMORY_NOT_FOUND", "Memory not found.")
    db.delete(row)
    db.commit()
    return {"deleted": True}


def _handoff(db, principal, agent_id, handoff_id):
    row = db.get(HumanHandoffRequest, handoff_id)
    if row is None or row.tenant_id != principal.tenant_id or row.agent_id != agent_id:
        raise PublicAPIError(404, "HANDOFF_NOT_FOUND", "Handoff not found.")
    owned_public_session(db, principal, agent_id, row.session_id)
    return row


@router.get("/handoffs/{handoff_id}")
def handoff(
    agent_id: str,
    handoff_id: str,
    principal: PublicPrincipal = Depends(require_scopes("sessions:read")),
    db: Session = Depends(get_session),
):
    from app.api.chat import human_handoff_read

    return human_handoff_read(_handoff(db, principal, agent_id, handoff_id))


@router.post("/handoffs/{handoff_id}/reply")
def reply_handoff(
    agent_id: str,
    handoff_id: str,
    body: dict,
    principal: PublicPrincipal = Depends(require_scopes("sessions:write")),
    db: Session = Depends(get_session),
):
    row = _handoff(db, principal, agent_id, handoff_id)
    from app.api.chat import _apply_handoff_reply, human_handoff_read

    reply = body.get("reply")
    if not isinstance(reply, str) or not reply.strip() or len(reply) > 4000:
        raise PublicAPIError(400, "INVALID_REPLY", "A reply of 1-4000 characters is required.")
    if row.assignee_user_id not in {None, principal.actor_user.id}:
        raise PublicAPIError(
            403, "HANDOFF_ASSIGNEE_MISMATCH", "The handoff belongs to another assignee."
        )
    if row.status not in {"pending", "assigned"}:
        raise PublicAPIError(409, "HANDOFF_NOT_PENDING", "The handoff is not pending.")
    _apply_handoff_reply(
        db, row, reply.strip(), answered_by_user_id=principal.actor_user.id, source="web"
    )
    return human_handoff_read(row)


@router.post("/sessions/{session_id}/cancel")
def cancel(
    agent_id: str,
    session_id: str,
    body: dict,
    principal: PublicPrincipal = Depends(require_scopes("runs:cancel")),
    db: Session = Depends(get_session),
):
    owned_public_session(db, principal, agent_id, session_id)
    from app.api.chat import ChatTurnCancelRequest, cancel_chat_turn_endpoint

    return cancel_chat_turn_endpoint(
        session_id,
        ChatTurnCancelRequest(tenant_id=principal.tenant_id, turn_id=body.get("turn_id")),
        principal.actor_user,
        db,
    )
