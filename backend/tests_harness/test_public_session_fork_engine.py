"""Real delegated SOP execution, durable fork, then a new engine continuation."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.agents.branching import ensure_private_resource_binding
from app.db import get_session
from app.db.models import (
    APIClient,
    APICredential,
    APIJob,
    ChatSession,
    HarnessSessionCheckpoint,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Skill,
)
from app.public_api.app import create_public_api_app
from app.public_api.auth import generate_api_key
from app.public_api.jobs import run_job
from app.public_api.runtime import bind_actor
from app.session.session_schema import PlannedTaskFrame, TurnPlan
from staffdeck_harness.runtime.services import runtime_services
from tests_harness.test_platform_isolation_engine import _headers
from tests_harness.test_sealed_e2e_engine import _chunk, _seed
from tests_harness.test_sealed_e2e_engine import db as db  # noqa: PLC0414
from tests_harness.test_sealed_e2e_engine import fake_model as fake_model  # noqa: PLC0414
from tests_harness.test_sealed_e2e_engine import pytestmark as engine_marks

pytestmark = engine_marks


@pytest.mark.parametrize("cold", [False, True])
def test_fork_continues_old_sop_and_files_without_future_context(db, fake_model, monkeypatch, cold):
    # Shared long-term memory intentionally does not roll back with a branch;
    # keep its unrelated background model calls out of this turn-state test.
    monkeypatch.setattr(
        "app.core.agent_loop.AgentLoop._enqueue_memory_capture", lambda *a, **k: None
    )
    model, base = fake_model
    agent, owner = _seed(db, base)
    owner.role = "admin"
    sop = Skill(
        id="fork-sop",
        tenant_id=owner.tenant_id,
        skill_id="fork-flow",
        name="Fork flow",
        status="published",
        content_json={
            "start_node_id": "ask",
            "terminal_node_ids": ["ask"],
            "nodes": [
                {
                    "node_id": "ask",
                    "name": "Budget",
                    "type": "response",
                    "instruction": "Record budget and preserve the draft.",
                    "allowed_actions": ["answer_user"],
                }
            ],
            "edges": [],
        },
    )
    db.add_all([owner, sop])
    db.commit()
    ensure_private_resource_binding(db, owner.tenant_id, agent.id, "skill", sop.id)
    token, prefix, digest = generate_api_key()
    scopes = ["*", "platform:delegate"]
    db.add(
        APIClient(
            id="fork-platform",
            tenant_id=owner.tenant_id,
            name="Synthetic platform",
            created_by_user_id=owner.id,
            scopes_json=scopes,
            metadata_json={"platform_tenant_id": "platform_tenant"},
        )
    )
    db.add(
        APICredential(
            id="fork-key",
            tenant_id=owner.tenant_id,
            client_id="fork-platform",
            name="Test only",
            key_prefix=prefix,
            key_digest=digest,
            scopes_json=scopes,
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
    reads = []

    def reply(body):
        model.requests.append(body)
        messages = body.get("messages", [])
        current = next(
            (
                m
                for m in reversed(messages)
                if m.get("role") == "user" and "# 本次用户输入" in str(m.get("content"))
            ),
            {},
        )
        current_text = str(current.get("content", ""))
        phase = (
            "EDITED"
            if "EDITED" in current_text
            else "FUTURE"
            if "FUTURE" in current_text
            else "FIRST"
        )
        calls = {
            m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"
        }
        if not body.get("tools") or "submit-" + phase in calls:
            return [_chunk("done", {"content": "finished-" + phase}), _chunk("done", {}, "stop")]
        if "file-" + phase not in calls:
            name = "sandbox_execute"
            args = (
                {"tool": "read_file", "arguments": {"path": "report.txt"}}
                if phase == "EDITED"
                else {
                    "tool": "write_file",
                    "arguments": {
                        "path": "report.txt",
                        "content": "HISTORY_V1" if phase == "FIRST" else "FUTURE_SECRET_V2",
                    },
                }
            )
            identifier = "file-" + phase
        else:
            if phase == "EDITED":
                reads.append(str(calls["file-EDITED"]))
            name, identifier = "submit_step_result", "submit-" + phase
            args = {
                "status": "completed" if phase == "EDITED" else "awaiting_user",
                "reply_fragment": "reply-" + phase,
                "slot_updates": {
                    "budget": 20 if phase == "EDITED" else 99 if phase == "FUTURE" else 10
                },
            }
        return [
            _chunk(
                identifier,
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": identifier,
                            "type": "function",
                            "function": {
                                "name": "mcp__staffdeck__" + name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ]
                },
            ),
            _chunk(identifier, {}, "tool_calls"),
        ]

    monkeypatch.setattr(model, "reply", reply)

    def continue_plan(_planner, _message, session, *args, **kwargs):
        with Session(engine) as query:
            frame = query.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == session.id
                )
            ).first()
            return TurnPlan(
                decision="continue_active",
                task_frames=[
                    PlannedTaskFrame(
                        task_id=frame.task_id,
                        kind="sop",
                        decision="continue_active",
                        target_skill_id="fork-flow",
                        target_step_id="ask",
                    )
                ],
            )

    monkeypatch.setattr(
        "staffdeck_harness.runtime.model_phases.EngineTurnPlanner.plan", continue_plan
    )
    with TestClient(app) as client:

        def run(text, sid=None):
            response = client.post(
                "/agents/agent_1/runs",
                headers=_headers(token, "bob"),
                json={"input": text, **({"session_id": sid} if sid else {})},
            )
            assert response.status_code == 202, response.text
            bind_actor(db, owner)
            run_job(response.json()["id"], data_services=runtime_services(db))
            db.expire_all()
            job = db.get(APIJob, response.json()["id"])
            assert job.status == "succeeded", job.error_json
            return job

        first = run("/sop fork-flow FIRST")
        sid = first.session_id
        future = run("FUTURE", sid)
        pivot = (
            db.exec(select(HarnessTurnRecord).where(HarnessTurnRecord.client_turn_id == future.id))
            .one()
            .user_message_id
        )
        checkpoints = db.exec(
            select(HarnessSessionCheckpoint).where(HarnessSessionCheckpoint.session_id == sid)
        ).all()
        assert any(cp.before_message_id == pivot and cp.status == "ready" for cp in checkpoints)
        before_fork = len(model.requests)
        response = client.post(
            f"/agents/agent_1/sessions/{sid}:fork",
            headers={**_headers(token, "bob"), "Idempotency-Key": "engine-fork"},
            json={"beforeMessageId": pivot},
        )
        assert response.status_code == 201, response.text
        assert len(model.requests) == before_fork, (
            "Fork must not execute external effects or a model"
        )
        target = response.json()["sessionId"]
        db.expire_all()
        assert db.get(ChatSession, target).slots_json["budget"] == 10
        if cold:
            from staffdeck_harness.bridge.engine_host import reset_runtime

            reset_runtime()
        result = run("EDITED", target)
        assert "HISTORY_V1" in " ".join(reads), reads
        assert "FUTURE_SECRET_V2" not in json.dumps(model.requests[before_fork:])
        completed = db.exec(
            select(HarnessTaskFrameRecord).where(HarnessTaskFrameRecord.session_id == target)
        ).one()
        assert (
            completed.status == "completed"
            and completed.result_json["slot_updates"]["budget"] == 20
        )
        assert db.get(ChatSession, sid).slots_json["budget"] == 99
        assert result.session_id != sid
