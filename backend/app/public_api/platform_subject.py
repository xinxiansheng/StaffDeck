"""Explicit server-to-server delegation; a service key is never an end-user identity.

The platform authenticates/authorizes the human before forwarding this header.
Only a separately configured client AND key may delegate. Ordinary API keys,
including wildcard keys, cannot opt in by sending an arbitrary subject header.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

from sqlalchemy.exc import IntegrityError

from app.db.models import APIClient, APICredential, User
from app.public_api.errors import PublicAPIError

HEADER = "X-Platform-Subject"
DELEGATE_SCOPE = "platform:delegate"
PRIVATE_SCOPES = frozenset(
    {
        "sessions:read",
        "sessions:write",
        "runs:create",
        "runs:read",
        "runs:cancel",
        "jobs:read",
        "jobs:cancel",
        "memories:read",
        "memories:write",
    }
)


def decode_subject(value: str | None) -> dict | None:
    if value is None:
        return None
    try:
        if not value or len(value) > 4096:
            raise ValueError("Invalid length")
        data = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(data, dict)
            or set(data) != {"tenant_id", "user_id", "employee_id", "agent_id"}
            or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in data.values())
        ):
            raise ValueError("Invalid subject")
        return data
    except (ValueError, UnicodeError) as exc:
        raise PublicAPIError(
            403, "PLATFORM_SUBJECT_REQUIRED", "A valid platform subject is required."
        ) from exc


def bind_platform_subject(
    db, principal, client: APIClient, credential: APICredential, subject: dict | None
):
    configured = (client.metadata_json or {}).get("platform_tenant_id")
    enabled = DELEGATE_SCOPE in (client.scopes_json or []) and DELEGATE_SCOPE in (
        credential.scopes_json or []
    )
    if configured is None and not enabled and subject is None:
        db.info.pop("staffdeck_platform_principal", None)
        return principal
    if not configured or not enabled or not subject or subject.get("tenant_id") != configured:
        raise PublicAPIError(
            403, "PLATFORM_SUBJECT_REQUIRED", "Platform delegation is not authorized."
        )
    # Alternate identity providers must implement their own verified mapping; never project
    # a local shadow identity through a provider that has not accepted this delegation.
    from staffdeck_harness.runtime.control_auth import provider

    if provider() is not None:
        raise PublicAPIError(
            503,
            "PLATFORM_IDENTITY_UNAVAILABLE",
            "This identity provider does not support platform delegation.",
        )
    from app.security.permissions import is_admin_user

    if not is_admin_user(principal.actor_user):
        raise PublicAPIError(
            403,
            "DELEGATION_ADMIN_REQUIRED",
            "The delegation client must be owned by the deployment administrator.",
        )
    from app.public_api.auth import enforce_agent_access

    enforce_agent_access(principal, subject["agent_id"])
    digest = hashlib.sha256(
        json.dumps(
            [client.tenant_id, configured, subject["user_id"]], separators=(",", ":")
        ).encode()
    ).hexdigest()
    actor_id = "platform_" + digest[:32]
    actor = db.get(User, actor_id)
    if actor is None:
        actor = User(
            id=actor_id,
            tenant_id=client.tenant_id,
            username="platform_" + digest,
            display_name="Platform member",
            role="member",
            source="platform",
            password_hash="!",
        )
        db.add(actor)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        actor = db.get(User, actor_id)
    if (
        actor is None
        or actor.tenant_id != client.tenant_id
        or actor.source != "platform"
        or actor.role != "member"
    ):
        raise PublicAPIError(403, "PLATFORM_IDENTITY_INVALID", "Platform identity is unavailable.")
    from app.public_api.runtime import bind_actor

    bind_actor(db, actor)
    delegated = replace(
        principal,
        actor_user=actor,
        agent_id=subject["agent_id"],
        scopes=frozenset(s for s in PRIVATE_SCOPES if principal.can(s)),
        manageable_agent_ids=frozenset(),
        platform_subject=dict(subject),
    )
    db.info["staffdeck_platform_principal"] = delegated
    return delegated


def scoped_key(principal, value: str) -> str:
    if principal.platform_subject is None:
        return value
    raw = json.dumps([principal.platform_subject, value], sort_keys=True, separators=(",", ":"))
    return "platform:" + hashlib.sha256(raw.encode()).hexdigest()


def principal_for_job(db, job):
    from app.public_api.auth import principal_for_credential

    captured = (job.request_json or {}).get("_runtime_context") or {}
    principal = principal_for_credential(
        db, job.credential_id, subject=captured.get("platform_subject")
    )
    actor_id = captured.get("actor_id")
    if (
        principal.tenant_id != job.tenant_id
        or actor_id
        and actor_id != principal.actor_user.id
        or principal.platform_subject is not None
        and not actor_id
    ):
        raise PublicAPIError(
            403, "JOB_IDENTITY_CHANGED", "The admitted job identity is unavailable."
        )
    return principal


def owns_job(db, principal, job) -> bool:
    """No tenant-wide reads, including through generic /jobs and legacy records."""
    if (
        job.tenant_id != principal.tenant_id
        or job.credential_id != principal.credential_id
        or principal.agent_id
        and job.agent_id != principal.agent_id
    ):
        return False
    captured = (job.request_json or {}).get("_runtime_context") or {}
    if principal.platform_subject is not None:
        return (
            captured.get("actor_id") == principal.actor_user.id
            and captured.get("platform_subject") == principal.platform_subject
        )
    # Existing ordinary keys may read their own legacy jobs, never delegated jobs.
    return (
        not captured.get("platform_subject")
        and captured.get("actor_id", principal.actor_user.id) == principal.actor_user.id
    )


def delegated_security_context(db, context, security, actor):
    """Revalidate the service grant while keeping the human's member identity."""
    from app.db.models import ChatSession
    from app.public_api.auth import principal_for_credential
    from staffdeck_harness.contracts.errors import PermissionDenied

    current = db.info.get("staffdeck_platform_principal")
    session = db.get(ChatSession, context.session_id) if context.session_id else None
    saved = (session.channel_target_json or {}) if session else {}
    subject = current.platform_subject if current else saved.get("_platform_subject")
    credential_id = current.credential_id if current else saved.get("_platform_credential")
    if not subject or not credential_id:
        raise PermissionDenied("平台身份缺少可信授权来源")
    try:
        live = principal_for_credential(db, credential_id, subject=subject)
    except PublicAPIError as exc:
        raise PermissionDenied("平台授权已失效") from exc
    if (
        live.actor_user.id != actor.id
        or live.tenant_id != context.tenant_id
        or live.agent_id != context.staff_id
        or session
        and session.user_id != actor.id
    ):
        raise PermissionDenied("平台身份范围不一致")
    return replace(
        security,
        attributes={
            **security.attributes,
            "platform_agent_id": live.agent_id,
            "platform_credential_id": credential_id,
        },
    )


def platform_handoff_owner(db, row):
    """A private platform decision stays with its original human, never the service owner."""
    from app.db.models import ChatSession
    from app.public_api.auth import principal_for_credential

    session = db.get(ChatSession, row.session_id)
    saved = (session.channel_target_json or {}) if session else {}
    subject = saved.get("_platform_subject")
    if not subject:
        return None
    principal = principal_for_credential(db, saved.get("_platform_credential"), subject=subject)
    if (
        session.tenant_id != row.tenant_id
        or session.agent_id != row.agent_id
        or principal.actor_user.id != session.user_id
    ):
        raise PublicAPIError(
            403, "HANDOFF_SUBJECT_MISMATCH", "The private handoff identity is unavailable."
        )
    return principal.actor_user.id
