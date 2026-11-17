from __future__ import annotations

import mimetypes
import re
from typing import Any
import threading
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from app.core import AgentLoop
from app.core.cancellation import cancel_chat_turn
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.db import get_session
from app.db.models import (
    APIJob,
    AgentEvent,
    HarnessInvocationRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Message,
)
from app.harness import HarnessArtifactAccessError, normalize_harness_artifact_path, open_harness_artifact
from app.public_api.auth import PublicPrincipal, enforce_agent_access, require_scopes
from app.public_api.errors import PublicAPIError, RunExecutionFailure
from app.public_api.idempotency import replay_idempotent_response, store_idempotent_response
from app.public_api.images import resolve_run_attachments
from app.public_api.jobs import (
    create_job,
    ensure_not_cancelled,
    job_read,
    register_job_handler,
    update_job,
)
from app.public_api.jobs import stream_job_events
from app.public_api.schemas import AgentRunCreate, PublicSessionCreate
from app.public_api.sessions import create_public_session_row, ensure_public_agent, owned_public_session
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse


router = APIRouter(tags=["runs"])

_TRACE_EVENT_MAP = {
    "stream_status": "run.status",
    "stream_delta": "run.output.delta",
    "stream_replace": "run.output.replace",
    "stream_end": "run.output.completed",
    "stream_cancelled": "run.cancelled",
    "turn_plan_created": "run.plan",
    "router_decision_created": "run.intent",
    "task_frame_started": "run.task_frame.started",
    "task_frame_finished": "run.task_frame.finished",
    "task_frame_completed": "run.task_frame.completed",
    "task_frame_dependency_waiting": "run.task_frame.waiting",
    "task_frame_dependencies_released": "run.task_frame.released",
    "capability_search_completed": "run.capability.search",
    "capability_described": "run.capability.described",
    "harness_tool_completed": "run.capability.completed",
    "harness_tool_result": "run.capability.completed",
    "harness_action_created": "run.action.started",
    "harness_action_failed": "run.action.failed",
    "harness_step_timeout": "run.sop.step.timeout",
    "knowledge_result": "run.citation",
    "tool_result": "run.tool.completed",
    "skill_state": "run.sop.state",
    "step_result": "run.sop.step",
    "general_skill_trace": "run.skill.trace",
    "general_skill_run_finished": "run.skill.completed",
    "agent_loop_continued": "run.loop.continued",
    "agent_loop_completed": "run.loop.completed",
    "error_occurred": "run.failed",
    "human_handoff_created": "handoff.created",
}
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "credentials",
    "error_traceback",
    "headers",
    "raw_cot",
    "system_prompt",
    "platform_context",
    "context_injection",
    "tool_credentials",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    if isinstance(value, str) and len(value) > 8000:
        return value[:8000] + "…"
    return value


def _job_actor(db: Session, job: APIJob):
    from app.public_api.platform_subject import principal_for_job
    principal = principal_for_job(db, job)
    if not principal.can("runs:create"):
        raise PublicAPIError(403, "RUN_AUTHORIZATION_REVOKED", "Run authorization has been revoked.")
    ensure_public_agent(db, principal, str(job.agent_id))
    return principal


_PLATFORM_RUN_OPTIONS = {"model_config_id", "platform_context"}


def _platform_run_options(db: Session, principal: PublicPrincipal, metadata: dict) -> tuple[str | None, str | None]:
    """Server-only options: validate at admission and again after durable recovery."""
    if not _PLATFORM_RUN_OPTIONS.intersection(metadata):
        return None, None
    if principal.platform_subject is None:
        raise PublicAPIError(403, "PLATFORM_DELEGATION_REQUIRED", "Runtime overrides require a trusted platform subject.")
    model_id = metadata.get("model_config_id")
    if "model_config_id" in metadata:
        if not isinstance(model_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", model_id):
            raise PublicAPIError(400, "PLATFORM_MODEL_INVALID", "Invalid runtime model identifier.")
        from app.llm.model_config_resolver import resolve_model_config_for_runtime
        try:
            resolve_model_config_for_runtime(db, principal.tenant_id, model_id)
        except HTTPException as exc:
            raise PublicAPIError(exc.status_code, str(exc.detail), "The selected runtime model is unavailable.") from exc
    context = metadata.get("platform_context")
    if context is not None and not isinstance(context, str):
        raise PublicAPIError(400, "PLATFORM_CONTEXT_INVALID", "Runtime context must be text.")
    normalized_context = context.strip()[:2000] if context is not None else None
    return model_id, normalized_context or None


def _message_matches_run(message: Message, client_turn_id: str, turn_id: str | None) -> bool:
    metadata = dict(message.metadata_json or {})
    identities = {
        str(metadata.get("client_turn_id") or "").strip(),
        str(metadata.get("turn_id") or "").strip(),
        str(metadata.get("user_message_id") or "").strip(),
    }
    identities.discard("")
    return client_turn_id in identities or bool(turn_id and turn_id in identities)


def _latest_artifacts(
    db: Session,
    tenant_id: str,
    session_id: str,
    client_turn_id: str,
    turn_id: str | None,
) -> list[dict[str, Any]]:
    rows = db.exec(
        select(Message).where(
            Message.tenant_id == tenant_id,
            Message.session_id == session_id,
            Message.role == "assistant",
        )
    ).all()
    artifacts: list[dict[str, Any]] = []
    for row in rows:
        if not _message_matches_run(row, client_turn_id, turn_id):
            continue
        for artifact in (row.metadata_json or {}).get("harness_artifacts") or []:
            if isinstance(artifact, dict):
                artifacts.append(_redact(dict(artifact)))
    return artifacts[-100:]


def _run_artifacts(db: Session, job: APIJob) -> list[dict[str, Any]]:
    if not job.session_id:
        return []
    turn = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == job.tenant_id,
            HarnessTurnRecord.session_id == job.session_id,
            HarnessTurnRecord.client_turn_id == job.id,
        )
    ).first()
    return _latest_artifacts(
        db, job.tenant_id, job.session_id, job.id, turn.user_message_id if turn else None,
    )


@register_job_handler("run")
def execute_run(db: Session, job: APIJob) -> dict[str, Any]:
    principal = _job_actor(db, job)
    actor = principal.actor_user
    payload = dict(job.request_json or {})
    metadata = dict(payload.get("metadata") or {})
    model_id, context = _platform_run_options(db, principal, metadata)
    attachments = resolve_run_attachments(payload.get("attachments") or [],
                                         principal=principal, agent_id=str(job.agent_id))
    session_id = str(payload.get("session_id") or "").strip() or None
    stateless = payload.get("session_mode") == "stateless"
    if not session_id:
        session, _ = create_public_session_row(
            db,
            principal,
            str(job.agent_id),
            PublicSessionCreate(
                external_session_id=None if stateless else payload.get("external_session_id"),
                external_user_id=payload.get("external_user_id"),
                title=str(payload.get("input") or "")[:80],
                # Run-only options must not become public external-session metadata.
                metadata={key: value for key, value in metadata.items() if key not in _PLATFORM_RUN_OPTIONS},
            ),
        )
        session_id = session.id
    owned_public_session(db, principal, str(job.agent_id), session_id)
    job.session_id = session_id
    db.add(job)
    update_job(
        db,
        job,
        stage="executing",
        progress=0.1,
        event_type="run.executing",
        event_data={"session_id": session_id, "engine": "harness_v3"},
    )
    request = ChatTurnRequest(
        tenant_id=job.tenant_id,
        session_id=session_id,
        agent_id=job.agent_id,
        client_turn_id=job.id,
        user_id=actor.id,
        message=str(payload.get("input") or ""),
        attachments=attachments,
        channel="public_api",
        model_config_id=model_id,
        context_injection=context,
    )
    if principal.platform_subject is not None:
        metadata = payload.get("metadata") or {}
        allowed = metadata.get("allowed_platform_tools")
        if allowed is not None and (not isinstance(allowed, list) or len(allowed) > 200
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name)
                       for name in allowed)):
            raise PublicAPIError(400, "PLATFORM_TOOLS_INVALID", "Invalid platform tool allowlist.")
        request._platform_tools = allowed
    seen_event_ids: set[str] = set()
    worker_done = threading.Event()
    worker_result: dict[str, Any] = {}
    from staffdeck_harness.runtime.services import runtime_services
    data_services = runtime_services(db)

    def execute_harness() -> None:
        try:
            with data_services.session(None, purpose="public-run-worker") as worker_db:
                from app.public_api.platform_subject import principal_for_job
                principal_for_job(worker_db, worker_db.get(APIJob, job.id))
                for item in AgentLoop(worker_db).handle_turn_stream(request):
                    if item.get("event") == "error" and isinstance(item.get("data"), dict):
                        worker_result["stream_error"] = item["data"]
                    if item.get("event") != "complete":
                        continue
                    data = item.get("data")
                    if isinstance(data, dict):
                        worker_result["response"] = ChatTurnResponse.model_validate(data)
                if "response" not in worker_result:
                    # Failed turns intentionally emit `error` instead of `complete`.
                    # Recover only this job's durable terminal response; never the
                    # latest message/session response, which may belong to another run.
                    terminal = worker_db.exec(select(HarnessTurnRecord).where(
                        HarnessTurnRecord.tenant_id == job.tenant_id,
                        HarnessTurnRecord.session_id == session_id,
                        HarnessTurnRecord.client_turn_id == job.id,
                    )).first()
                    if terminal and terminal.status in {"completed", "failed"} and terminal.response_json:
                        worker_result["response"] = ChatTurnResponse.model_validate(terminal.response_json)
                    elif worker_result.get("stream_error"):
                        error = worker_result["stream_error"]
                        raise RunExecutionFailure(str(error.get("code") or "HARNESS_RUNTIME_ERROR"),
                                                  str(error.get("message") or "本次运行失败。"))
                    else:
                        raise RuntimeError("Harness stream ended without a complete response")
        except Exception as exc:  # noqa: BLE001 - re-raised at persistent job boundary.
            worker_result["error"] = exc
        finally:
            worker_done.set()

    thread = threading.Thread(target=execute_harness, name=f"public-run-{job.id}", daemon=True)
    thread.start()
    try:
        while not worker_done.is_set():
            ensure_not_cancelled(db, job)
            _relay_agent_events(db, job, session_id, seen_event_ids)
            worker_done.wait(0.1)
    except BaseException:
        # Keep the queued job's assembly lease until its child loop really exits.
        # A cancelled relay must not leave a worker using unloaded modules.
        cancel_chat_turn(session_id, job.id)
        while not worker_done.wait(0.1):
            pass
        thread.join()
        raise
    thread.join(timeout=1)
    _relay_agent_events(db, job, session_id, seen_event_ids)
    if "error" in worker_result:
        raise worker_result["error"]
    result = ChatTurnResponse.model_validate(worker_result.get("response"))
    response_json = result.model_dump(mode="json")
    state = dict(response_json.get("session_state") or {})
    turn = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == job.tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.client_turn_id == job.id,
        )
    ).first()
    source_turn_id = turn.user_message_id if turn else None
    frames = db.exec(
        select(HarnessTaskFrameRecord)
        .where(
            HarnessTaskFrameRecord.tenant_id == job.tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.source_turn_id == source_turn_id,
        )
        .order_by(HarnessTaskFrameRecord.sequence)
    ).all() if source_turn_id else []
    invocations = db.exec(
        select(HarnessInvocationRecord)
        .where(
            HarnessInvocationRecord.tenant_id == job.tenant_id,
            HarnessInvocationRecord.session_id == session_id,
            HarnessInvocationRecord.task_id.in_([frame.task_id for frame in frames]),
        )
        .order_by(HarnessInvocationRecord.created_at)
    ).all() if frames else []
    if result.runtime_error_code:
        causes = [{"code": result.runtime_error_code, "message": result.reply[:2000]}]
        seen_codes = {result.runtime_error_code}
        for error in [*(frame.error_json for frame in frames),
                      *((invocation.result_json or {}).get("error") for invocation in invocations)]:
            if not isinstance(error, dict) or not error.get("code") or error["code"] in seen_codes:
                continue
            seen_codes.add(error["code"])
            causes.append({"code": str(error["code"]), "message": str(error.get("message") or "")[:2000]})
        raise RunExecutionFailure(result.runtime_error_code, result.reply, causes=_redact(causes[:20]))
    assistants = db.exec(
        select(Message)
        .where(
            Message.tenant_id == job.tenant_id,
            Message.session_id == session_id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at.desc())
    ).all()
    assistant = next(
        (
            row
            for row in assistants
            if _message_matches_run(row, job.id, source_turn_id)
        ),
        None,
    )
    assistant_metadata = dict(assistant.metadata_json or {}) if assistant else {}
    return {
        "run_id": job.id,
        "agent_id": job.agent_id,
        "session_id": None if stateless else session_id,
        "reply": result.reply,
        "citations": _redact(list(assistant_metadata.get("knowledge_citations") or [])),
        "tool_calls": [
            {
                "call_id": invocation.call_id,
                "task_frame_id": invocation.task_id,
                "name": invocation.tool_name,
                "status": invocation.status,
                "arguments": _redact(dict(invocation.arguments_json or {})),
                "result": _redact(dict(invocation.result_json or {})),
            }
            for invocation in invocations
        ],
        "task_results": [
            {
                "task_frame_id": frame.task_id,
                "kind": frame.kind,
                "status": frame.status,
                "sop_id": frame.skill_id,
                "step_id": frame.step_id,
                "result": _redact(dict(frame.result_json or {})),
                "error": _redact(dict(frame.error_json or {})),
            }
            for frame in frames
        ],
        "awaiting_input": state.get("awaiting_input"),
        "session_state": state,
        "artifacts": _latest_artifacts(
            db,
            job.tenant_id,
            session_id,
            job.id,
            source_turn_id,
        ),
    }


def _relay_agent_events(
    db: Session,
    job: APIJob,
    session_id: str,
    seen_event_ids: set[str],
) -> None:
    rows = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == job.tenant_id,
            AgentEvent.session_id == session_id,
        )
        .order_by(AgentEvent.created_at, AgentEvent.id)
    ).all()
    turn = db.exec(
        select(HarnessTurnRecord).where(
            HarnessTurnRecord.tenant_id == job.tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.client_turn_id == job.id,
        )
    ).first()
    source_turn_id = turn.user_message_id if turn else None
    for event in rows:
        if event.id in seen_event_ids:
            continue
        seen_event_ids.add(event.id)
        payload = dict(event.payload_json or {})
        identities = {
            str(payload.get("client_turn_id") or "").strip(),
            str(payload.get("turn_id") or "").strip(),
            str(payload.get("user_message_id") or "").strip(),
        }
        identities.discard("")
        if job.id not in identities and not (source_turn_id and source_turn_id in identities):
            continue
        public_type = _TRACE_EVENT_MAP.get(event.event_type)
        if public_type:
            event_data = _redact(payload)
            if public_type == "run.output.completed":
                assistants = db.exec(
                    select(Message)
                    .where(
                        Message.tenant_id == job.tenant_id,
                        Message.session_id == session_id,
                        Message.role == "assistant",
                    )
                    .order_by(Message.created_at.desc())
                ).all()
                assistant = next(
                    (
                        row
                        for row in assistants
                        if _message_matches_run(row, job.id, source_turn_id)
                    ),
                    None,
                )
                citations = (
                    list((assistant.metadata_json or {}).get("knowledge_citations") or [])
                    if assistant
                    else []
                )
                if citations:
                    event_data["citations"] = _redact(citations)
            update_job(
                db,
                job,
                event_type=public_type,
                event_data=event_data,
            )


@router.post("/agents/{agent_id}/runs", response_model=dict, status_code=202)
def create_run_route(
    agent_id: str,
    body: AgentRunCreate,
    request: Request,
    response: Response,
    principal: PublicPrincipal = Depends(require_scopes("runs:create")),
    db: Session = Depends(get_session),
) -> dict:
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    _platform_run_options(db, principal, body.metadata)
    if body.session_id:
        owned_public_session(db, principal, agent_id, body.session_id)
    replay = replay_idempotent_response(db, principal, request, body.model_dump(mode="json"))
    if replay:
        response.status_code = replay[0]
        return replay[1]
    resolve_run_attachments(body.attachments, principal=principal, agent_id=agent_id)
    job = create_job(
        db,
        principal,
        kind="run",
        request_payload=body.model_dump(mode="json"),
        agent_id=agent_id,
    )
    payload = job_read(job).model_dump(mode="json")
    store_idempotent_response(
        db,
        principal,
        request,
        body.model_dump(mode="json"),
        payload,
        status_code=202,
        resource_id=job.id,
    )
    return payload


@router.post("/agents/{agent_id}/runs:stream", status_code=200)
def create_run_stream_route(
    agent_id: str,
    body: AgentRunCreate,
    request: Request,
    principal: PublicPrincipal = Depends(require_scopes("runs:create")),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    """Create a durable run and stream its public events in the same request."""
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    _platform_run_options(db, principal, body.metadata)
    if body.session_id:
        owned_public_session(db, principal, agent_id, body.session_id)
    request_payload = body.model_dump(mode="json")
    replay = replay_idempotent_response(db, principal, request, request_payload)
    if replay:
        run_id = str(replay[1].get("id") or "")
        job = _owned_run(db, principal, run_id)
    else:
        resolve_run_attachments(body.attachments, principal=principal, agent_id=agent_id)
        job = create_job(
            db,
            principal,
            kind="run",
            request_payload=request_payload,
            agent_id=agent_id,
        )
        payload = job_read(job).model_dump(mode="json")
        store_idempotent_response(
            db,
            principal,
            request,
            request_payload,
            payload,
            status_code=202,
            resource_id=job.id,
        )
    stream = stream_job_events(job.id, request, 0, None, principal, db)
    stream.headers["X-Run-ID"] = job.id
    stream.headers["Cache-Control"] = "no-cache, no-transform"
    stream.headers["X-Accel-Buffering"] = "no"
    return stream


def _owned_run(db: Session, principal: PublicPrincipal, run_id: str) -> APIJob:
    row = db.get(APIJob, run_id)
    from app.public_api.platform_subject import owns_job
    if not row or row.kind != "run" or not owns_job(db, principal, row):
        raise PublicAPIError(404, "RUN_NOT_FOUND", "Run not found.")
    if principal.agent_id and row.agent_id != principal.agent_id:
        raise PublicAPIError(404, "RUN_NOT_FOUND", "Run not found.")
    return row


@router.get("/runs/{run_id}", response_model=dict)
def get_run(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> dict:
    return job_read(_owned_run(db, principal, run_id), db).model_dump(mode="json")


@router.get("/runs/{run_id}/result", response_model=dict)
def get_run_result(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_run(db, principal, run_id)
    if row.status != "succeeded":
        raise PublicAPIError(409, "RUN_NOT_SUCCEEDED", "The run has not succeeded.")
    return dict(row.result_json or {})


@router.get("/runs/{run_id}/events")
def stream_run_events(
    run_id: str,
    request: Request,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    _owned_run(db, principal, run_id)
    return stream_job_events(run_id, request, after, last_event_id, principal, db)


@router.post("/runs/{run_id}:cancel", response_model=dict)
def cancel_run(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:cancel")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_run(db, principal, run_id)
    if row.status not in {"succeeded", "failed", "cancelled"}:
        row.cancel_requested = True
        if row.session_id:
            cancel_chat_turn(row.session_id, row.id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return job_read(row).model_dump(mode="json")


@router.get("/runs/{run_id}/artifacts", response_model=dict)
def list_run_artifacts(
    run_id: str,
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> dict:
    row = _owned_run(db, principal, run_id)
    return {"data": _run_artifacts(db, row)}


@router.get("/runs/{run_id}/artifacts/{task_frame_id}")
def download_run_artifact(
    run_id: str,
    task_frame_id: str,
    path: str = Query(..., min_length=1),
    principal: PublicPrincipal = Depends(require_scopes("runs:read")),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    row = _owned_run(db, principal, run_id)
    if not row.session_id:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
    try:
        normalized = normalize_harness_artifact_path(path)
    except HarnessArtifactAccessError as exc:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.") from exc
    artifact = next(
        (
            item
            for item in _run_artifacts(db, row)
            if item.get("type") == "workspace_file"
            and str(item.get("task_frame_id") or "") == task_frame_id
            and item.get("path") == normalized
        ),
        None,
    )
    if not artifact:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.")
    try:
        opened = open_harness_artifact(
            harness_task_workspace_path(
                tenant_id=row.tenant_id,
                session_id=row.session_id,
                task_frame_id=task_frame_id,
                db=db,
            ),
            normalized,
        )
    except (HarnessArtifactAccessError, OSError) as exc:
        raise PublicAPIError(404, "ARTIFACT_NOT_FOUND", "Artifact not found.") from exc
    filename = _safe_artifact_download_name(
        str(artifact.get("display_name") or opened.filename)
    )
    fallback_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    fallback_filename = (fallback_filename or "artifact")[:120]
    media_type = (
        str(artifact.get("content_type") or "").strip()
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    return StreamingResponse(
        opened.iter_bytes(),
        media_type=media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; '
                f"filename*=UTF-8''{quote(filename, safe='')}"
            ),
            "Content-Length": str(opened.size),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(opened.close),
    )


def _safe_artifact_download_name(filename: str) -> str:
    cleaned = "".join(
        character
        for character in str(filename).strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    return cleaned[:180] or "artifact"
