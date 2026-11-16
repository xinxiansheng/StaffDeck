"""One server credential must not collapse platform users into its owner."""

import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.db.models import AgentProfile, APIClient, APICredential, APIJob, Tenant, User
from app.public_api.app import create_public_api_app
from app.public_api.auth import generate_api_key


@pytest.fixture
def platform(monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    token, prefix, digest = generate_api_key()
    with Session(engine) as db:
        db.add(Tenant(id="sd_tenant", name="Isolated test"))
        db.add(
            User(
                id="service_owner",
                tenant_id="sd_tenant",
                username="service",
                role="admin",
                password_hash="!",
            )
        )
        db.add(
            AgentProfile(
                id="agent_test",
                tenant_id="sd_tenant",
                name="Test employee",
                status="active",
                metadata_json={"owner_user_id": "service_owner"},
            )
        )
        db.add(
            APIClient(
                id="platform_client",
                tenant_id="sd_tenant",
                name="Platform",
                created_by_user_id="service_owner",
                scopes_json=["*", "platform:delegate"],
                metadata_json={"platform_tenant_id": "platform_tenant"},
            )
        )
        db.add(
            APICredential(
                id="platform_key",
                tenant_id="sd_tenant",
                client_id="platform_client",
                name="Test only",
                key_prefix=prefix,
                key_digest=digest,
                scopes_json=["*", "platform:delegate"],
            )
        )
        db.commit()
    app = create_public_api_app()

    def db_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = db_session
    monkeypatch.setattr("app.public_api.app.engine", engine)
    monkeypatch.setattr("app.public_api.jobs.enqueue_async_job", lambda *a, **k: "queued")
    with TestClient(app) as client:
        yield client, engine, token
    engine.dispose()


def headers(token, user="alice", tenant="platform_tenant", **extra):
    scope = {
        "tenant_id": tenant,
        "user_id": user,
        "employee_id": "employee_test",
        "agent_id": "agent_test",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(scope).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer {token}", "X-Platform-Subject": encoded, **extra}


def create_session(client, token, user="alice", **body):
    response = client.post(
        "/agents/agent_test/sessions",
        headers=headers(token, user),
        json={"title": "Private session", **body},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_shared_credential_separates_session_identity_and_direct_reads(platform):
    client, _, token = platform
    a = create_session(client, token)
    b = create_session(client, token, "bob")
    assert a != b
    assert client.get(f"/agents/agent_test/sessions/{a}", headers=headers(token)).status_code == 200
    assert (
        client.get(f"/agents/agent_test/sessions/{a}", headers=headers(token, "bob")).status_code
        == 404
    )


def test_shared_credential_idempotency_and_external_session_ids_are_subject_scoped(platform):
    client, _, token = platform
    results = []
    for user in ["alice", "bob", "alice"]:
        result = client.post(
            "/agents/agent_test/sessions",
            headers=headers(token, user, **{"Idempotency-Key": "same-key"}),
            json={"title": "Private", "external_session_id": "same-session"},
        )
        assert result.status_code == 201, result.text
        results.append(result.json()["id"])
    assert results[0] == results[2]
    assert results[0] != results[1]


def test_run_result_and_generic_job_routes_enforce_the_same_subject(platform):
    client, engine, token = platform
    sid = create_session(client, token)
    created = client.post(
        "/agents/agent_test/runs",
        headers=headers(token),
        json={"input": "Private test", "session_id": sid},
    )
    assert created.status_code == 202, created.text
    rid = created.json()["id"]
    with Session(engine) as db:
        row = db.get(APIJob, rid)
        row.status = "succeeded"
        row.result_json = {"reply": "ALICE_PRIVATE_RESULT"}
        db.add(row)
        db.commit()
    for path in [
        f"/runs/{rid}",
        f"/runs/{rid}/result",
        f"/jobs/{rid}",
        f"/jobs/{rid}/result",
        f"/runs/{rid}/artifacts",
    ]:
        assert client.get(path, headers=headers(token)).status_code == 200
        denied = client.get(path, headers=headers(token, "bob"))
        assert denied.status_code == 404, path
        assert "ALICE_PRIVATE_RESULT" not in denied.text


@pytest.mark.parametrize("scope_headers", [{}, {"X-Platform-Subject": "invalid"}])
def test_delegating_credential_never_falls_back_to_service_owner(platform, scope_headers):
    client, _, token = platform
    response = client.post(
        "/agents/agent_test/sessions",
        headers={"Authorization": f"Bearer {token}", **scope_headers},
        json={},
    )
    assert response.status_code == 403


def test_platform_tenant_cannot_be_changed_in_a_subject_header(platform):
    client, _, token = platform
    response = client.post(
        "/agents/agent_test/sessions", headers=headers(token, tenant="another_platform"), json={}
    )
    assert response.status_code == 403


def test_private_detail_reset_upload_and_native_memory_are_owner_scoped(platform):
    from app.db.models import ChatSession, MemoryRecord, Message
    from app.memory.service import MemoryService
    from app.session.attachment_store import read_staged_chat_attachment
    from app.session.session_schema import ChatAttachmentRead

    client, engine, token = platform
    a = create_session(client, token)
    b = create_session(client, token, "bob")
    with Session(engine) as db:
        alice, bob = db.get(ChatSession, a).user_id, db.get(ChatSession, b).user_id
        db.add(
            Message(
                id="alice-message",
                tenant_id="sd_tenant",
                session_id=a,
                role="assistant",
                content="ALICE_PRIVATE_MESSAGE",
            )
        )
        db.add(
            MemoryRecord(
                id="alice-memory",
                tenant_id="sd_tenant",
                user_id=alice,
                kind="preference",
                content="ALICE_PRIVATE_MEMORY",
                session_id=a,
                metadata_json={"agent_id": "agent_test"},
            )
        )
        db.add(
            MemoryRecord(
                id="bob-memory",
                tenant_id="sd_tenant",
                user_id=bob,
                kind="preference",
                content="BOB_PRIVATE_MEMORY",
                session_id=b,
                metadata_json={"agent_id": "agent_test"},
            )
        )
        db.commit()
        assert all(
            "ALICE" not in row.content
            for row in MemoryService(db).context_memories("sd_tenant", bob, agent_id="agent_test")
        )
        assert any(
            "ALICE" in row.content
            for row in MemoryService(db).context_memories("sd_tenant", alice, agent_id="agent_test")
        )
    detail = f"/agents/agent_test/sessions/{a}/detail"
    assert "ALICE_PRIVATE_MESSAGE" in client.get(detail, headers=headers(token)).text
    assert client.get(detail, headers=headers(token, "bob")).status_code == 404
    assert (
        client.post(
            f"/agents/agent_test/sessions/{a}/reset", headers=headers(token, "bob")
        ).status_code
        == 404
    )
    upload = client.post(
        "/agents/agent_test/attachments",
        headers=headers(token),
        files={"files": ("private.txt", b"ALICE_PRIVATE_FILE", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    file = ChatAttachmentRead.model_validate(upload.json()[0])
    assert (
        read_staged_chat_attachment(file, tenant_id="sd_tenant", user_id=alice)
        == b"ALICE_PRIVATE_FILE"
    )
    assert read_staged_chat_attachment(file, tenant_id="sd_tenant", user_id=bob) is None
    a_mem = client.get("/agents/agent_test/memories", headers=headers(token))
    b_mem = client.get("/agents/agent_test/memories", headers=headers(token, "bob"))
    assert a_mem.status_code == b_mem.status_code == 200
    assert "ALICE_PRIVATE_MEMORY" in a_mem.text and "BOB_PRIVATE_MEMORY" not in a_mem.text
    assert "ALICE_PRIVATE_MEMORY" not in b_mem.text and "BOB_PRIVATE_MEMORY" in b_mem.text
    assert (
        client.delete(
            "/agents/agent_test/memories/alice-memory", headers=headers(token, "bob")
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/agents/agent_test/memories/alice-memory", headers=headers(token)
        ).status_code
        == 200
    )


def test_queued_identity_survives_new_database_session_and_revocation_stops_it(platform):
    from app.public_api.errors import PublicAPIError
    from app.public_api.platform_subject import principal_for_job

    client, engine, token = platform
    sid = create_session(client, token)
    result = client.post(
        "/agents/agent_test/runs",
        headers=headers(token),
        json={"input": "test", "session_id": sid, "metadata": {"user_id": "bob"}},
    )
    rid = result.json()["id"]
    with Session(engine) as db:
        job = db.get(APIJob, rid)
        admitted = job.request_json["_runtime_context"]["actor_id"]
        assert admitted != "service_owner"
    with Session(engine) as db:
        assert principal_for_job(db, db.get(APIJob, rid)).actor_user.id == admitted
        credential = db.get(APICredential, "platform_key")
        credential.status = "revoked"
        db.add(credential)
        db.commit()
    with Session(engine) as db, pytest.raises(PublicAPIError, match="revoked"):
        principal_for_job(db, db.get(APIJob, rid))
    assert client.get(f"/runs/{rid}", headers=headers(token)).status_code == 401


def test_wildcard_key_cannot_opt_into_delegation_or_manage_via_delegated_subject(platform):
    client, engine, token = platform
    assert client.get("/credentials", headers=headers(token)).status_code in {403, 404, 405}
    with Session(engine) as db:
        key = db.get(APICredential, "platform_key")
        key.scopes_json = ["*"]
        db.add(key)
        db.commit()
    assert (
        client.post("/agents/agent_test/sessions", headers=headers(token), json={}).status_code
        == 403
    )


@pytest.mark.parametrize(
    "change",
    [
        "key_revoked",
        "scope_removed",
        "client_disabled",
        "tool_disabled",
        "tool_type",
        "server_changed",
        "argument_override",
    ],
)
def test_live_gateway_checks_reject_mutations_after_admission(platform, change):
    from dataclasses import replace
    from types import SimpleNamespace

    from app.db.models import ChatSession, MCPServer, Tool
    from app.public_api.auth import principal_for_credential
    from app.public_api.platform_subject import decode_subject
    from staffdeck_harness.capabilities.platform_isolation import require_invocation
    from staffdeck_harness.contracts.errors import PermissionDenied
    from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation

    client, engine, token = platform
    sid = create_session(client, token)
    subject = decode_subject(headers(token)["X-Platform-Subject"])
    with Session(engine) as db:
        db.add(MCPServer(id="gateway", tenant_id="sd_tenant", name="platform_employeetest"))
        db.add(
            Tool(
                id="approved",
                tenant_id="sd_tenant",
                name="approved",
                tool_type="mcp",
                method="POST",
                url="",
                mcp_server_id="gateway",
            )
        )
        db.commit()
        actor = principal_for_credential(db, "platform_key", subject=subject).actor_user
        inv = ModuleInvocation(
            "test-inv",
            "tool",
            "tool.invoke/v1",
            {"tool_id": "approved"},
            InvocationContext("sd_tenant", "agent_test", actor.id, sid, "turn-test", "web"),
            binding_id="approved",
        )
        snapshot = SimpleNamespace(
            tenant_id="sd_tenant",
            session_policy={
                "platform_gateway_server": "platform_employeetest",
                "platform_allowed_tools": None,
                "platform_subject": subject,
                "platform_credential": "platform_key",
            },
        )
        # Positive control: the live employee gateway is usable.
        require_invocation(db, snapshot, inv)
        # Another transaction changes authority while this execution retains cached rows.
        with Session(engine) as writer:
            if change in {"key_revoked", "scope_removed"}:
                row = writer.get(APICredential, "platform_key")
                if change == "key_revoked":
                    row.status = "revoked"
                else:
                    row.scopes_json = ["platform:delegate", "sessions:read"]
            elif change == "client_disabled":
                row = writer.get(APIClient, "platform_client")
                row.status = "disabled"
            elif change in {"tool_disabled", "tool_type"}:
                row = writer.get(Tool, "approved")
                if change == "tool_disabled":
                    row.enabled = False
                else:
                    row.tool_type = "http"
            elif change == "server_changed":
                row = writer.get(MCPServer, "gateway")
                row.name = "another_employee_gateway"
            else:
                row = writer.get(ChatSession, sid)
                inv = replace(inv, arguments={"tool_id": "native-business-tool"})
            writer.add(row)
            writer.commit()
        with pytest.raises(PermissionDenied):
            require_invocation(db, snapshot, inv)


def test_ordinary_member_cannot_self_grant_delegation_or_take_over_service_credentials(platform):
    client, engine, _ = platform
    token, prefix, digest = generate_api_key()
    with Session(engine) as db:
        db.add(
            User(
                id="native-member",
                tenant_id="sd_tenant",
                username="member",
                role="member",
                password_hash="!",
            )
        )
        db.add(
            APIClient(
                id="member-client",
                tenant_id="sd_tenant",
                name="Member client",
                created_by_user_id="native-member",
                scopes_json=["*"],
            )
        )
        db.add(
            APICredential(
                id="member-key",
                tenant_id="sd_tenant",
                client_id="member-client",
                name="Member key",
                key_prefix=prefix,
                key_digest=digest,
                scopes_json=["*"],
            )
        )
        db.commit()
    member_headers = {"Authorization": "Bearer " + token}
    # Members retain self-service management of their own ordinary clients.
    response = client.post(
        "/api-clients",
        headers=member_headers,
        json={"name": "member-owned", "scopes": ["sessions:read"]},
    )
    assert response.status_code == 201, response.text
    visible = client.get("/api-clients", headers=member_headers)
    assert "platform_client" not in visible.text
    attempts = [
        (
            "POST",
            "/api-clients",
            {
                "name": "forged",
                "scopes": ["platform:delegate"],
                "metadata": {"platform_tenant_id": "platform_tenant"},
            },
        ),
        ("PATCH", "/api-clients/member-client", {"scopes": ["*", "platform:delegate"]}),
        (
            "PATCH",
            "/api-clients/member-client",
            {"metadata": {"platform_tenant_id": "platform_tenant"}},
        ),
        ("PATCH", "/api-clients/platform_client", {"status": "archived"}),
        ("GET", "/api-clients/platform_client/credentials", None),
        ("POST", "/api-clients/platform_client/credentials", {"name": "stolen", "scopes": ["*"]}),
        ("POST", "/credentials/platform_key:rotate", None),
        ("POST", "/credentials/platform_key:revoke", None),
    ]
    for method, path, body in attempts:
        response = client.request(method, path, headers=member_headers, json=body)
        assert response.status_code == 403, (method, path, response.status_code, response.text)


def test_private_handoff_keeps_initiator_and_accepts_assigned_reply(platform, monkeypatch):
    from app.db.models import ChatSession, HumanHandoffRequest
    from app.public_api.auth import principal_for_credential
    from app.public_api.platform_subject import decode_subject
    from staffdeck_harness.handoff.core import for_session
    from staffdeck_harness.runtime.staff_ownership import handoff_context

    client, engine, token = platform
    sid = create_session(client, token)
    with Session(engine) as db:
        principal = principal_for_credential(
            db, "platform_key", subject=decode_subject(headers(token)["X-Platform-Subject"])
        )
        session = db.get(ChatSession, sid)
        session.channel_target_json = {
            **session.channel_target_json,
            "_platform_allowed_tools": ["original-tool"],
        }
        db.add(session)
        db.commit()
        core = for_session(db, session)
        context = handoff_context(db, session, core.guard.profile)
        row = core.create(
            context,
            session,
            pending_question="ALICE_PRIVATE_DECISION",
            context_summary="Private",
            metadata={"step_assignee_user_id": "service_owner"},
            notify_channel="feishu",
        )
        handoff_id = row.id
        assert row.assignee_user_id == principal.actor_user.id
        assert row.status == "assigned"
        assert row.notify_message_id is None
        assert row.resume_payload_json["channel_target"]["_platform_allowed_tools"] == [
            "original-tool"
        ]
    path = f"/agents/agent_test/handoffs/{handoff_id}"
    assert "ALICE_PRIVATE_DECISION" in client.get(path, headers=headers(token)).text
    assert client.get(path, headers=headers(token, "bob")).status_code == 404
    assert (
        client.post(
            path + "/reply", headers=headers(token, "bob"), json={"reply": "not authorized"}
        ).status_code
        == 404
    )
    resumed = []
    monkeypatch.setattr("app.api.chat._resume_human_handoff_async", resumed.append)
    response = client.post(path + "/reply", headers=headers(token), json={"reply": "approved"})
    assert response.status_code == 200, response.text
    assert resumed == [handoff_id]
    with Session(engine) as db:
        row = db.get(HumanHandoffRequest, handoff_id)
        assert row.status == "answered"
        assert row.resume_payload_json["answered_by_user_id"] == principal.actor_user.id
    cancel = f"/agents/agent_test/sessions/{sid}/cancel"
    assert (
        client.post(
            cancel, headers=headers(token, "bob"), json={"turn_id": "test-turn"}
        ).status_code
        == 404
    )
    assert (
        client.post(cancel, headers=headers(token), json={"turn_id": "test-turn"}).status_code
        == 200
    )


def test_handoff_resume_cannot_gain_a_later_gateway_capability(platform):
    from app.db.models import ChatSession, MCPServer, Tool
    from app.session.session_schema import ChatTurnRequest
    from staffdeck_harness.capabilities.platform_isolation import restrict_snapshot
    from staffdeck_harness.composition.compiler import (
        CapabilityGrant,
        CompositionSnapshot,
        HookPlan,
    )

    client, engine, token = platform
    sid = create_session(client, token)
    with Session(engine) as db:
        session = db.get(ChatSession, sid)
        db.add(MCPServer(id="gateway", tenant_id="sd_tenant", name="platform_employeetest"))
        for name in ("original", "later"):
            db.add(
                Tool(
                    id=name,
                    tenant_id="sd_tenant",
                    name=name,
                    tool_type="mcp",
                    method="POST",
                    url="",
                    mcp_server_id="gateway",
                )
            )
        db.commit()
        snapshot = CompositionSnapshot(
            "snapshot",
            "sd_tenant",
            "agent_test",
            {},
            None,
            {},
            {},
            tuple(
                CapabilityGrant("tool.invoke/v1", "tool", name, name)
                for name in ("original", "later")
            ),
            (),
            HookPlan({}),
            (),
            None,
            (),
        )
        request = ChatTurnRequest(
            tenant_id="sd_tenant",
            agent_id="agent_test",
            session_id=sid,
            user_id=session.user_id,
            message="initial",
            channel="web",
        )
        request._platform_tools = ["original"]
        first = restrict_snapshot(db, snapshot, request, session)
        assert [g.resource_id for g in first.grants] == ["original"]
        db.expire_all()
        # The fresh composition has both tools; the original admitted ceiling survives recovery.
        resumed = request.model_copy(update={"channel": "human_handoff_resume"})
        resumed._platform_tools = None
        limited = restrict_snapshot(db, snapshot, resumed, db.get(ChatSession, sid))
        assert [g.resource_id for g in limited.grants] == ["original"]


def _ordinary_run_key(engine):
    token, prefix, digest = generate_api_key()
    with Session(engine) as db:
        db.add(APIClient(id="ordinary_client", tenant_id="sd_tenant", name="Ordinary",
                         created_by_user_id="service_owner", scopes_json=["*"]))
        db.add(APICredential(id="ordinary_key", tenant_id="sd_tenant", client_id="ordinary_client",
                            name="Test", key_prefix=prefix, key_digest=digest, scopes_json=["*"]))
        db.commit()
    return {"Authorization": "Bearer " + token}


def _runtime_model(engine, **changes):
    from app.db.models import ModelConfig
    from app.llm.model_protocols import model_config_fingerprint

    with Session(engine) as db:
        model = ModelConfig(id="selected_model", tenant_id="sd_tenant", name="Selected",
                            model="selected", api_key_encrypted="test-only", trust_status="verified")
        db.add(model)
        db.flush()
        model.verified_fingerprint = model_config_fingerprint(
            api_protocol=model.api_protocol, base_url=model.base_url, model=model.model,
            key_revision=model.key_revision, protocol_options={},
            security_revision=model.security_revision,
        )
        for key, value in changes.items():
            setattr(model, key, value)
        db.add(model)
        db.commit()
    return "selected_model"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("metadata", [{"model_config_id": "selected_model"}, {"platform_context": "hidden"}])
def test_runtime_overrides_require_delegation_before_enqueuing(platform, monkeypatch, stream, metadata):
    from sqlmodel import select
    from starlette.responses import StreamingResponse

    client, engine, _ = platform
    auth = _ordinary_run_key(engine)
    monkeypatch.setattr("app.public_api.runs.stream_job_events",
                        lambda *a: StreamingResponse(iter(["event: done\n\n"])))
    response = client.post("/agents/agent_test/runs" + (":stream" if stream else ""),
                           headers=auth, json={"input": "test", "metadata": metadata})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "PLATFORM_DELEGATION_REQUIRED"
    with Session(engine) as db:
        assert not db.exec(select(APIJob)).all()
    # Existing ordinary API clients can still run without platform-only options.
    assert client.post("/agents/agent_test/runs", headers=auth, json={"input": "normal"}).status_code == 202


@pytest.mark.parametrize("metadata", [
    {"model_config_id": "../model"}, {"model_config_id": "x" * 129},
    {"model_config_id": 1}, {"model_config_id": ""},
    {"platform_context": {"system": "hidden"}},
])
def test_runtime_override_malformed_values_are_not_silently_ignored(platform, metadata):
    client, _, token = platform
    response = client.post("/agents/agent_test/runs", headers=headers(token),
                           json={"input": "test", "metadata": metadata})
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("changes,status", [
    ({"tenant_id": "another_tenant"}, 404), ({"enabled": False}, 409),
    ({"trust_status": "unverified", "config_revision": 2}, 409),
    ({"verified_fingerprint": "stale"}, 409),
])
def test_runtime_model_uses_existing_tenant_and_readiness_rules(platform, changes, status):
    client, engine, token = platform
    model_id = _runtime_model(engine, **changes)
    response = client.post("/agents/agent_test/runs", headers=headers(token),
                           json={"input": "test", "metadata": {"model_config_id": model_id}})
    assert response.status_code == status, response.text


def test_runtime_model_missing_is_rejected(platform):
    client, _, token = platform
    response = client.post("/agents/agent_test/runs", headers=headers(token),
                           json={"input": "test", "metadata": {"model_config_id": "missing"}})
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("change", ["disable_model", "revoke_delegation"])
def test_queued_runtime_options_recheck_live_authorization(platform, monkeypatch, change):
    from app.db.models import ModelConfig
    from app.public_api.runs import execute_run
    from app.public_api.errors import PublicAPIError

    client, engine, token = platform
    model_id = _runtime_model(engine)
    response = client.post("/agents/agent_test/runs", headers=headers(token),
                           json={"input": "test", "metadata": {"model_config_id": model_id,
                                                                  "platform_context": "hidden"}})
    assert response.status_code == 202, response.text
    def unexpected(*a, **kw):
        pytest.fail("An invalidated queued run reached the model")
    monkeypatch.setattr("app.public_api.runs.AgentLoop", unexpected)
    with Session(engine) as db:
        row = db.get(ModelConfig, model_id) if change == "disable_model" else db.get(APIClient, "platform_client")
        if change == "disable_model":
            row.enabled = False
        else:
            row.scopes_json = ["*"]
        db.add(row)
        db.commit()
    with Session(engine) as db:
        with pytest.raises(PublicAPIError) as caught:
            execute_run(db, db.get(APIJob, response.json()["id"]))
        assert caught.value.status_code in (403, 409)


def test_legacy_ordinary_job_cannot_gain_runtime_options_after_upgrade(platform, monkeypatch):
    from app.public_api.runs import execute_run
    from app.public_api.errors import PublicAPIError

    client, engine, _ = platform
    auth = _ordinary_run_key(engine)
    response = client.post("/agents/agent_test/runs", headers=auth, json={"input": "old run"})
    with Session(engine) as db:
        job = db.get(APIJob, response.json()["id"])
        job.request_json = {**job.request_json, "metadata": {"platform_context": "old untrusted option"}}
        db.add(job)
        db.commit()
        with pytest.raises(PublicAPIError) as caught:
            execute_run(db, job)
        assert caught.value.code == "PLATFORM_DELEGATION_REQUIRED"


@pytest.mark.parametrize("trust", ["verified", "legacy_trusted"])
def test_delegated_stream_admission_preserves_model_compatibility(platform, monkeypatch, trust):
    from starlette.responses import StreamingResponse

    client, engine, token = platform
    model_id = _runtime_model(engine, trust_status=trust)
    monkeypatch.setattr("app.public_api.runs.stream_job_events",
                        lambda *a: StreamingResponse(iter(["event: queued\n\n"])))
    response = client.post("/agents/agent_test/runs:stream", headers=headers(token),
                           json={"input": "test", "metadata": {"model_config_id": model_id,
                                                                  "platform_context": "hidden"}})
    assert response.status_code == 200, response.text
    with Session(engine) as db:
        job = db.get(APIJob, response.headers["X-Run-ID"])
        assert job.request_json["metadata"]["model_config_id"] == model_id
        assert job.request_json["_runtime_context"]["platform_subject"]["user_id"] == "alice"
