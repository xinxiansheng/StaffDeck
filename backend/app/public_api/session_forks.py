"""Fork an owned session from immutable server checkpoints, with atomic receipts."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from app.core.harness_session_cleanup import remove_harness_session_workspace
from app.core.harness_session_lease import HarnessSessionLeaseStore
from app.core.harness_session_lock import (
    HarnessSessionBusy,
    acquire_harness_session,
    release_harness_session,
)
from app.core.session_checkpoints import (
    CheckpointUnavailable,
    checkpoint_for_message,
    restore_checkpoint,
)
from app.db.models import (
    APIIdempotencyRecord,
    APIJob,
    ChatSession,
    ExternalSessionBinding,
    HarnessTurnRecord,
    Message,
    utc_now,
)
from app.public_api.errors import PublicAPIError
from app.public_api.idempotency import request_fingerprint
from app.public_api.platform_subject import scoped_key


class PublicSessionFork(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    before_message_id: str = Field(alias="beforeMessageId", min_length=1, max_length=200)


def fork_public_session(db, principal, request, agent_id, session_id, body):
    from app.public_api.sessions import owned_public_session
    from staffdeck_harness.runtime.session_binding import bind_session

    source, _ = owned_public_session(db, principal, agent_id, session_id)
    # Validate the runtime realm without mutating the original session.
    bind_session(db, ChatSession.model_validate(source.model_dump()))
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > 200 or not principal.credential_id:
        raise PublicAPIError(
            400, "IDEMPOTENCY_KEY_REQUIRED", "分叉需要 1～200 字符的 Idempotency-Key。"
        )
    key = scoped_key(principal, key)
    fingerprint = request_fingerprint(body.model_dump(by_alias=True))

    def replay():
        row = db.exec(
            select(APIIdempotencyRecord).where(
                APIIdempotencyRecord.tenant_id == principal.tenant_id,
                APIIdempotencyRecord.credential_id == principal.credential_id,
                APIIdempotencyRecord.method == request.method,
                APIIdempotencyRecord.path == request.url.path,
                APIIdempotencyRecord.idempotency_key == key,
            )
        ).first()
        if row:
            if row.request_hash != fingerprint:
                raise PublicAPIError(
                    409, "IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同分叉位置。"
                )
            owned_public_session(db, principal, agent_id, row.resource_id)
            return dict(row.response_json)
        return None

    previous = replay()
    if previous:
        return previous
    message = db.get(Message, body.before_message_id)
    if not message or message.tenant_id != source.tenant_id or message.session_id != source.id:
        raise PublicAPIError(404, "MESSAGE_NOT_FOUND", "消息不存在。")
    if message.role != "user":
        raise PublicAPIError(422, "FORK_USER_MESSAGE_REQUIRED", "只能从用户消息之前分叉。")
    lock = lease = None
    leases = HarnessSessionLeaseStore(db)
    target_id = (
        "session_fork_"
        + hashlib.sha256(
            json.dumps(
                [
                    principal.tenant_id,
                    principal.actor_user.id,
                    principal.credential_id,
                    request.url.path,
                    key,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:32]
    )
    try:
        lock = acquire_harness_session(source.id)
        lease = leases.acquire(source)
        source, _ = owned_public_session(db, principal, agent_id, session_id)
        previous = replay()
        if previous:
            return previous
        jobs = db.exec(
            select(APIJob).where(
                APIJob.tenant_id == source.tenant_id,
                APIJob.agent_id == source.agent_id,
                APIJob.status.in_(("queued", "running")),
            )
        ).all()
        bindings = db.exec(
            select(ExternalSessionBinding).where(
                ExternalSessionBinding.tenant_id == source.tenant_id,
                ExternalSessionBinding.session_id == source.id,
            )
        ).all()
        active_job = any(
            job.session_id == source.id
            or (job.request_json or {}).get("session_id") == source.id
            or (
                not (job.request_json or {}).get("session_id")
                and (job.request_json or {}).get("session_mode") != "stateless"
                and ((job.request_json or {}).get("_runtime_context") or {}).get("actor_id")
                == source.user_id
                and any(
                    binding.credential_id == job.credential_id
                    and (job.request_json or {}).get("external_session_id")
                    == (binding.metadata_json or {}).get(
                        "_platform_external_id", binding.external_session_id
                    )
                    for binding in bindings
                )
            )
            for job in jobs
        )
        active_turn = db.exec(
            select(HarnessTurnRecord.id).where(
                HarnessTurnRecord.tenant_id == source.tenant_id,
                HarnessTurnRecord.session_id == source.id,
                HarnessTurnRecord.status.in_(("started", "finalizing")),
                HarnessTurnRecord.lease_expires_at > utc_now(),
            )
        ).first()
        if active_job or active_turn:
            raise PublicAPIError(409, "SESSION_BUSY", "原会话仍在运行或排队，请结束后重试。")
        checkpoint = checkpoint_for_message(db, source, body.before_message_id)
        if db.get(ChatSession, target_id) is not None:
            raise PublicAPIError(
                409, "FORK_RECEIPT_CONFLICT", "分叉记录不一致，请保留幂等键联系维护方。"
            )
        # A process can die after restoring files but before committing SQLite.
        # The deterministic private destination makes the same-key retry safe.
        remove_harness_session_workspace(tenant_id=source.tenant_id, session_id=target_id, db=db)
        target, copied = restore_checkpoint(db, source, checkpoint, target_id, principal)
        payload = {
            "sessionId": target.id,
            "forkedFromSessionId": source.id,
            "beforeMessageId": body.before_message_id,
            "copied": copied,
            "externalEffectsReverted": False,
            "requiresNewConfirmation": True,
        }
        db.add(
            APIIdempotencyRecord(
                tenant_id=principal.tenant_id,
                credential_id=principal.credential_id,
                method=request.method,
                path=request.url.path,
                idempotency_key=key,
                request_hash=fingerprint,
                response_json=payload,
                status_code=201,
                resource_id=target.id,
                expires_at=datetime.max,
            )
        )
        leases.renew(lease)
        db.commit()
        return payload
    except HarnessSessionBusy as exc:
        raise PublicAPIError(409, "SESSION_BUSY", str(exc)) from exc
    except CheckpointUnavailable as exc:
        db.rollback()
        if db.get(ChatSession, target_id) is None:
            remove_harness_session_workspace(
                tenant_id=source.tenant_id, session_id=target_id, db=db
            )
        raise PublicAPIError(409, exc.code, str(exc)) from exc
    except (OSError, zipfile.BadZipFile) as exc:
        db.rollback()
        if db.get(ChatSession, target_id) is None:
            remove_harness_session_workspace(
                tenant_id=source.tenant_id, session_id=target_id, db=db
            )
        raise PublicAPIError(
            503, "FORK_STORAGE_FAILED", "分叉写入失败，可用相同幂等键重试。"
        ) from exc
    except Exception:
        db.rollback()
        if db.get(ChatSession, target_id) is None:
            remove_harness_session_workspace(
                tenant_id=source.tenant_id, session_id=target_id, db=db
            )
        raise
    finally:
        try:
            leases.release(lease)
        finally:
            release_harness_session(source.id, lock)
