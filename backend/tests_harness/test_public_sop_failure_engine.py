"""Real public job/turn/SOP/Node pipeline; only model decisions are scripted."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.agents.branching import ensure_private_resource_binding
from app.db import get_session
from app.db.models import (
    AgentEvent,
    APIClient,
    APICredential,
    APIJob,
    APIJobEvent,
    HarnessAgentLoopRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    MCPServer,
    Skill,
    Tool,
)
from app.public_api.app import create_public_api_app
from app.public_api.auth import generate_api_key
from app.public_api.jobs import run_job
from app.public_api.runtime import bind_actor
from staffdeck_harness.runtime.services import runtime_services
from tests_harness.test_platform_isolation_engine import _headers
from tests_harness.test_sealed_e2e_engine import _chunk, _seed
from tests_harness.test_sealed_e2e_engine import db as sealed_db
from tests_harness.test_sealed_e2e_engine import fake_model as sealed_model
from tests_harness.test_sealed_e2e_engine import pytestmark as engine_marks

db = sealed_db
fake_model = sealed_model
pytestmark = engine_marks


@pytest.mark.parametrize("submission,expected", [("failed", "NOT_ALLOWED"), ("handoff", "HANDOFF_NOT_ALLOWED")])
def test_public_sop_failure_keeps_current_reply_and_real_causes(db, fake_model, monkeypatch, submission, expected):
    model, base = fake_model
    agent, owner = _seed(db, base)
    owner.role = "admin"
    agent.harness_max_actions = 16
    tool_name = "word_revision_create" if submission == "failed" else "legal_contract_review_start"
    tool = Tool(id="restricted-tool", tenant_id=owner.tenant_id, name=tool_name, tool_type="mcp",
        method="POST", url="", mcp_server_id="gateway", config_json={"tool_name": tool_name},
        input_schema={"type": "object"}, allowed_skills_json=["another-sop"])
    sop = Skill(id="revision-sop", tenant_id=owner.tenant_id, skill_id="clause-modification",
        name="Clause modification", status="published", content_json={
            "start_node_id": "prepare", "terminal_node_ids": ["deliver"], "nodes": [
                {"node_id": "prepare", "name": "Prepare", "type": "response", "instruction": "PREPARE_REVISION",
                 "allowed_actions": ["answer_user"]},
                {"node_id": "deliver", "name": "Deliver", "type": "response", "instruction": "DELIVER_REVISION",
                 "allowed_actions": ["answer_user", "call_tool:" + tool_name],
                 "capability_refs": {"tool_ids": [tool.id]}}],
            "edges": [{"source_node_id": "prepare", "next_node_id": "deliver"}]})
    db.add_all([owner, agent, tool, sop, MCPServer(id="gateway", tenant_id=owner.tenant_id,
        name="platform_employee1", transport="streamable_http", url="http://127.0.0.1:9/mcp")])
    db.commit()
    for kind, resource in (("tool", tool.id), ("skill", sop.id)):
        ensure_private_resource_binding(db, owner.tenant_id, agent.id, kind, resource)
    token, prefix, digest = generate_api_key()
    scopes = ["*", "platform:delegate"]
    db.add(APIClient(id="platform", tenant_id=owner.tenant_id, name="Synthetic platform",
        created_by_user_id=owner.id, scopes_json=scopes, metadata_json={"platform_tenant_id": "platform_tenant"}))
    db.add(APICredential(id="platform-key", tenant_id=owner.tenant_id, client_id="platform",
        name="Test only", key_prefix=prefix, key_digest=digest, scopes_json=scopes))
    db.commit()
    engine = db.get_bind()
    app = create_public_api_app()
    def sessions():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    monkeypatch.setattr("app.public_api.app.engine", engine)
    monkeypatch.setattr("app.public_api.jobs.enqueue_async_job", lambda *a, **k: "queued")
    upstream = []
    def forbidden_transport(*args, **kwargs):
        upstream.append(True)
        raise AssertionError("Skill whitelist rejection must happen before transport")
    monkeypatch.setattr("app.tools.tool_executor.execute_mcp_tool", forbidden_transport)
    phase = []
    original_reply = model.reply
    failure_reply = "本步工具权限不足，未生成修订稿。" if submission == "failed" else "本步工具权限不足，需要人工协助。"
    def reply(body):
        if "TurnPlanner" in json.dumps(body.get("messages", [])):
            return original_reply(body)
        model.requests.append(body)
        if not body.get("tools"):
            return [_chunk("response", {"role": "assistant", "content": failure_reply}), _chunk("response", {}, "stop")]
        messages = body.get("messages", [])
        current = next((m for m in reversed(messages) if m.get("role") == "user"
                        and "# 本次用户输入" in str(m.get("content"))), {})
        delivering = "DELIVER_REVISION" in json.dumps(current.get("content", ""))
        receipts = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        if "sop-result" in receipts or not delivering and "sop-intro" in receipts:
            return [_chunk("done", {"role": "assistant", "content": failure_reply}), _chunk("done", {}, "stop")]
        if not delivering:
            call_id, name, arguments = "sop-intro", "submit_step_result", {
                "status": "completed", "reply_fragment": "PREVIOUS_TRANSITION：进入 Word 修订稿生成。", "next_step_id": "deliver"}
        elif "sop-tool" not in receipts:
            call_id, name, arguments = "sop-tool", "tool_invoke", {"tool_id": tool.id, "arguments": {}}
        else:
            call_id, name, arguments = "sop-result", "submit_step_result", {"status": submission, "reply_fragment": failure_reply}
        phase.append(name)
        return [_chunk("sop", {"role": "assistant", "tool_calls": [{"index": 0, "id": call_id,
            "type": "function", "function": {"name": "mcp__staffdeck__" + name, "arguments": json.dumps(arguments)}}]}),
            _chunk("sop", {}, "tool_calls")]
    monkeypatch.setattr(model, "reply", reply)
    with TestClient(app) as client:
        created = client.post("/agents/agent_1/runs", headers=_headers(token, "bob"),
            json={"input": "/sop clause-modification 请生成修订稿"})
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        bind_actor(db, owner)
        run_job(run_id, data_services=runtime_services(db))
        db.expire_all()
        job = db.get(APIJob, run_id)
        assert phase == ["submit_step_result", "tool_invoke", "submit_step_result"]
        assert not upstream
        assert job.status == "failed", job.result_json
        assert job.error_json["code"] == expected, job.error_json
        assert failure_reply in job.error_json["reply"]
        assert "PREVIOUS_TRANSITION" not in job.error_json["reply"]
        assert "NOT_ALLOWED" in {cause["code"] for cause in job.error_json["causes"]}
        assert "Harness stream ended" not in str(job.error_json)
        assert job.retryable is False
        turn = db.exec(select(HarnessTurnRecord).where(HarnessTurnRecord.client_turn_id == run_id)).one()
        assert turn.status == "failed" and turn.response_json["runtime_error_code"] == expected
        assert failure_reply in turn.response_json["reply"]
        frames = db.exec(select(HarnessTaskFrameRecord).where(HarnessTaskFrameRecord.source_turn_id == turn.user_message_id)).all()
        assert len(frames) == 1 and frames[0].status == "failed", [f.result_json for f in frames]
        logical = db.get(HarnessAgentLoopRecord, frames[0].agent_loop_id)
        assert logical.status == "failed"
        events = db.exec(select(APIJobEvent).where(APIJobEvent.job_id == run_id)).all()
        assert not any(e.event_type == "run.succeeded" for e in events)
        assert any(e.event_type == "run.failed" and e.data_json.get("code") == expected for e in events)
        tool_events = db.exec(select(AgentEvent).where(AgentEvent.session_id == job.session_id)).all()
        assert "NOT_ALLOWED" in str([e.payload_json for e in tool_events])
        detail = client.get(f"/runs/{run_id}", headers=_headers(token, "bob"))
        assert detail.status_code == 200 and failure_reply in detail.text
        assert client.get(f"/runs/{run_id}", headers=_headers(token, "alice")).status_code == 404
        run_job(run_id, data_services=runtime_services(db))
        assert len(phase) == 3, "A failed job must not automatically repeat the SOP"
