"""Real public API, durable jobs, Node engine and MCP; only the LLM is scripted locally."""

import json
import os
import shlex

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_session
from app.db.models import APIClient, APICredential, APIJob, ChatSession, MemoryRecord
from app.public_api.app import create_public_api_app
from app.public_api.auth import generate_api_key
from app.public_api.jobs import run_job
from app.public_api.runtime import bind_actor
from staffdeck_harness.runtime.services import runtime_services
from tests_harness.test_sealed_e2e_engine import _chunk, _seed
from tests_harness.test_sealed_e2e_engine import db as sealed_db
from tests_harness.test_sealed_e2e_engine import fake_model as sealed_model
from tests_harness.test_sealed_e2e_engine import pytestmark as engine_marks

db = sealed_db
fake_model = sealed_model
pytestmark = engine_marks


def _headers(token, user):
    import base64

    raw = json.dumps(
        {
            "tenant_id": "platform_tenant",
            "user_id": user,
            "employee_id": "employee_1",
            "agent_id": "agent_1",
        }
    ).encode()
    return {
        "Authorization": "Bearer " + token,
        "X-Platform-Subject": base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }


def test_platform_private_turns_use_real_engine_and_deny_cross_owner_tools(
    db, fake_model, monkeypatch, tmp_path
):
    model, base = fake_model
    agent, service_owner = _seed(db, base)
    service_owner.role = "admin"
    db.add(service_owner)
    db.commit()
    token, prefix, digest = generate_api_key()
    db.add(
        APIClient(
            id="platform",
            tenant_id="tenant_demo",
            name="Test platform",
            created_by_user_id=service_owner.id,
            scopes_json=["*", "platform:delegate"],
            metadata_json={"platform_tenant_id": "platform_tenant"},
        )
    )
    db.add(
        APICredential(
            id="platform-key",
            tenant_id="tenant_demo",
            client_id="platform",
            name="Ephemeral test",
            key_prefix=prefix,
            key_digest=digest,
            scopes_json=["*", "platform:delegate"],
        )
    )
    db.commit()
    engine = db.get_bind()
    app = create_public_api_app()

    def sessions():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    monkeypatch.setattr("app.public_api.app.engine", engine)
    monkeypatch.setattr("app.public_api.jobs.enqueue_async_job", lambda *a, **k: "queued")
    with TestClient(app) as client:
        created = {}
        for user in ("alice", "bob"):
            response = client.post(
                "/agents/agent_1/sessions", headers=_headers(token, user), json={}
            )
            assert response.status_code == 201, response.text
            created[user] = response.json()["id"]
        alice_id = db.get(ChatSession, created["alice"]).user_id
        db.add(
            MemoryRecord(
                id="private-native-memory",
                tenant_id="tenant_demo",
                user_id=alice_id,
                kind="preference",
                content="ALICE_NATIVE_SECRET_7419",
                session_id=created["alice"],
                metadata_json={"agent_id": agent.id},
            )
        )
        db.commit()
        from app.core.harness_session_cleanup import harness_task_workspace_path

        private_file = (
            harness_task_workspace_path(
                tenant_id="tenant_demo",
                session_id=created["alice"],
                task_frame_id="alice-private-frame",
                db=db,
            )
            / "private.txt"
        )
        private_file.parent.mkdir(parents=True, exist_ok=True)
        private_file.write_text("ALICE_FILE_SECRET_8520")
        original = model.reply
        steps = [
            (
                "mcp__staffdeck__sandbox_execute",
                {"tool": "read_file", "arguments": {"path": str(private_file)}},
            ),
            (
                "mcp__staffdeck__knowledge_search",
                {"query": "退货", "knowledge_base_ids": ["kb_1"]},
            ),
            (
                "mcp__staffdeck__sandbox_execute",
                {"tool": "exec_command", "arguments": {"command": "cat " + str(private_file)}},
            ),
            (
                "mcp__staffdeck__sandbox_execute",
                {
                    "tool": "write_file",
                    "arguments": {"path": "own.txt", "content": "BOB_WORKSPACE_OK"},
                },
            ),
            (
                "mcp__staffdeck__sandbox_execute",
                {"tool": "read_file", "arguments": {"path": "own.txt"}},
            ),
        ]
        steps.extend(
            [
                (
                    "mcp__staffdeck__sandbox_execute",
                    {"tool": "exec_command", "arguments": {"command": "printf BOB_COMMAND_OK"}},
                ),
                (
                    "mcp__staffdeck__sandbox_execute",
                    {
                        "tool": "exec_command",
                        "arguments": {
                            "command": "/usr/bin/curl --max-time 2 -sS -H 'Content-Type: application/json' -d "
                            + shlex.quote(
                                json.dumps(
                                    {
                                        "messages": [
                                            {"role": "user", "content": "NETWORK_ESCAPE_ATTEMPT"}
                                        ]
                                    }
                                )
                            )
                            + " "
                            + base
                            + "/chat/completions"
                        },
                    },
                ),
            ]
        )
        executed = []

        def reply(body):
            if not body.get("tools"):
                return original(body)
            model.requests.append(body)
            text = json.dumps(body, ensure_ascii=False)
            assert "ALICE_NATIVE_SECRET_7419" not in text
            assert "ALICE_FILE_SECRET_8520" not in text
            index = len(executed)
            if index < len(steps):
                name, arguments = steps[index]
                executed.append(name)
                return [
                    _chunk(
                        "private-test",
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call-{index}",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                    ),
                    _chunk("private-test", {}, "tool_calls"),
                ]
            return [
                _chunk("private-test", {"role": "assistant", "content": "权限检查完成"}),
                _chunk("private-test", {}, "stop"),
            ]

        monkeypatch.setattr(model, "reply", reply)
        response = client.post(
            "/agents/agent_1/runs",
            headers=_headers(token, "bob"),
            json={"input": "验证私有资料隔离", "session_id": created["bob"]},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        bind_actor(db, service_owner)
        run_job(run_id, data_services=runtime_services(db))
        db.expire_all()
        job = db.get(APIJob, run_id)
        assert job.status == "succeeded", job.error_json
        assert len(executed) == 7
        result = client.get(f"/runs/{run_id}/result", headers=_headers(token, "bob"))
        assert result.status_code == 200
        assert "ALICE_" not in result.text
        assert (
            client.get(f"/runs/{run_id}/result", headers=_headers(token, "alice")).status_code
            == 404
        )
        # The real model received all three denial receipts, never the private file or memory.
        receipts = [
            m for r in model.requests for m in r.get("messages", []) if m.get("role") == "tool"
        ]
        assert len(receipts) >= 7
        receipt_text = json.dumps(receipts, ensure_ascii=False)
        assert "PATH_OUTSIDE_WORKSPACE" in receipt_text
        assert "BOB_WORKSPACE_OK" in receipt_text
        assert "平台运行只能通过当前员工的授权网关" in receipt_text
        if os.environ.get("STAFFDECK_SRT_RUNTIME"):
            assert "BOB_COMMAND_OK" in receipt_text
            assert "SANDBOX_UNAVAILABLE" not in receipt_text
        else:
            # A machine without an OS isolation backend must refuse execution.
            assert "BOB_COMMAND_OK" in receipt_text or "SANDBOX_UNAVAILABLE" in receipt_text
        assert not any(
            r.get("messages") == [{"role": "user", "content": "NETWORK_ESCAPE_ATTEMPT"}]
            for r in model.requests
        )
        (tmp_path / "denial-receipts.json").write_text(receipt_text)
        assert all("ALICE_" not in str(m) for m in receipts)

        # Positive control: the same engine receives Alice's own native memory.
        monkeypatch.setattr(model, "reply", original)
        model.requests.clear()
        response = client.post(
            "/agents/agent_1/runs",
            headers=_headers(token, "alice"),
            json={"input": "读取我的偏好", "session_id": created["alice"]},
        )
        assert response.status_code == 202, response.text
        alice_run = response.json()["id"]
        run_job(alice_run, data_services=runtime_services(db))
        db.expire_all()
        assert db.get(APIJob, alice_run).status == "succeeded"
        assert "ALICE_NATIVE_SECRET_7419" in json.dumps(model.requests)
        assert (
            client.get(f"/runs/{alice_run}/result", headers=_headers(token, "bob")).status_code
            == 404
        )


def test_platform_run_model_and_hidden_context_reach_real_engine_only(db, fake_model, monkeypatch):
    from sqlmodel import select

    from app.db.models import Message, ModelConfig
    from app.llm.model_protocols import model_config_fingerprint

    model, base = fake_model
    agent, service_owner = _seed(db, base)
    service_owner.role = "admin"
    default = db.get(ModelConfig, "model_1")
    default.enabled = False  # A silently dropped override cannot pass this test.
    selected = ModelConfig(
        id="selected_model", tenant_id="tenant_demo", name="Selected", model="selected",
        base_url=base, api_key_encrypted=default.api_key_encrypted, trust_status="verified",
    )
    db.add(default)
    db.add(selected)
    db.add(service_owner)
    db.flush()
    selected.verified_fingerprint = model_config_fingerprint(
        api_protocol=selected.api_protocol, base_url=base, model="selected",
        key_revision=selected.key_revision, protocol_options={}, security_revision=selected.security_revision,
    )
    token, prefix, digest = generate_api_key()
    scopes = ["*", "platform:delegate"]
    db.add(APIClient(id="platform", tenant_id="tenant_demo", name="Platform",
                     created_by_user_id=service_owner.id, scopes_json=scopes,
                     metadata_json={"platform_tenant_id": "platform_tenant"}))
    db.add(APICredential(id="platform-key", tenant_id="tenant_demo", client_id="platform",
                         name="Ephemeral", key_prefix=prefix, key_digest=digest, scopes_json=scopes))
    db.commit()
    engine = db.get_bind()
    app = create_public_api_app()

    def sessions():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    monkeypatch.setattr("app.public_api.jobs.enqueue_async_job", lambda *a, **k: "queued")
    hidden = "ENGINE_ONLY_CONTEXT_9837:" + "x" * 2100
    visible = "检查运行配置"
    with TestClient(app) as client:
        response = client.post("/agents/agent_1/runs", headers=_headers(token, "alice"), json={
            "input": visible, "external_session_id": "af2-private-session",
            "metadata": {"model_config_id": "selected_model", "platform_context": "  " + hidden + "  ",
                         "customer_label": "keep_me"},
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        bind_actor(db, service_owner)
        run_job(run_id, data_services=runtime_services(db))
        db.expire_all()
        job = db.get(APIJob, run_id)
        assert job.status == "succeeded", job.error_json
        assert model.requests
        assert all(body["model"] == "selected" for body in model.requests)
        text = json.dumps(model.requests, ensure_ascii=False)
        assert hidden[:2000] in text
        assert hidden[:2001] not in text
        messages = db.exec(select(Message).where(Message.session_id == job.session_id, Message.role == "user")).all()
        assert [message.content for message in messages] == [visible]
        session = client.get(f"/agents/agent_1/sessions/{job.session_id}", headers=_headers(token, "alice"))
        assert session.status_code == 200, session.text
        assert session.json()["metadata"]["customer_label"] == "keep_me"
        for path in (f"/agents/agent_1/sessions/{job.session_id}",
                     f"/agents/agent_1/sessions/{job.session_id}/detail",
                     f"/runs/{run_id}", f"/runs/{run_id}/result", f"/runs/{run_id}/events"):
            result = client.get(path, headers=_headers(token, "alice"))
            assert result.status_code == 200, result.text
            assert "ENGINE_ONLY_CONTEXT_9837" not in result.text
            assert "platform_context" not in result.text
