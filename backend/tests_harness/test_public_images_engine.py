"""Public multipart upload → owner reference → actual Node/model gateway image input."""

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import (
    APIClient,
    APICredential,
    APIJob,
    HarnessAgentLoopRecord,
    HarnessSessionCheckpoint,
    HarnessTaskFrameRecord,
)
from app.public_api.app import create_public_api_app
from app.public_api.auth import generate_api_key
from app.public_api.jobs import run_job
from app.public_api.runtime import bind_actor
from staffdeck_harness.runtime.services import runtime_services
from tests_harness.test_platform_isolation_engine import _headers
from tests_harness.test_sealed_e2e_engine import _ModelHandler, _seed
from tests_harness.test_sealed_e2e_engine import db as db  # noqa: PLC0414 -- pytest fixture re-export
from tests_harness.test_sealed_e2e_engine import fake_model as fake_model  # noqa: PLC0414 -- pytest fixture re-export
from tests_harness.test_sealed_e2e_engine import pytestmark as engine_marks

pytestmark = engine_marks
ENCODED = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG = base64.b64decode(ENCODED)


@pytest.mark.parametrize("reject_phase", [None, "plan", "step"])
def test_delegated_image_reaches_actual_model_or_fails_explicitly(
    db, fake_model, monkeypatch, reject_phase
):
    _model, base = fake_model
    _agent, owner = _seed(db, base)
    owner.role = "admin"
    db.add(owner)
    token, prefix, digest = generate_api_key()
    scopes = ["*", "platform:delegate"]
    db.add(
        APIClient(
            id="platform-images",
            tenant_id=owner.tenant_id,
            name="Synthetic platform",
            created_by_user_id=owner.id,
            scopes_json=scopes,
            metadata_json={"platform_tenant_id": "platform_tenant"},
        )
    )
    db.add(
        APICredential(
            id="image-key",
            tenant_id=owner.tenant_id,
            client_id="platform-images",
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
    received = []
    original_handler = _ModelHandler.do_POST

    def handle(handler):
        raw = handler.rfile.read(int(handler.headers.get("content-length") or 0))
        body = json.loads(raw)
        received.append(body)
        planning = "TurnPlanner" in json.dumps(body.get("messages", []))
        has_images = any(
            isinstance(m.get("content"), list)
            and any(p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict))
            for m in body.get("messages", [])
        )
        if has_images and reject_phase == ("plan" if planning else "step"):
            error = json.dumps(
                {
                    "error": {
                        "message": "image_url is unsupported by this model",
                        "type": "invalid_request_error",
                    }
                }
            ).encode()
            handler.send_response(400)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(error)))
            handler.end_headers()
            handler.wfile.write(error)
        else:
            handler.rfile = io.BytesIO(raw)
            original_handler(handler)

    monkeypatch.setattr(_ModelHandler, "do_POST", handle)
    with TestClient(app) as client:
        headers = _headers(token, "bob")
        uploaded = client.post(
            "/agents/agent_1/images",
            headers=headers,
            files=[("files", ("budget.png", PNG, "image/png"))],
        )
        assert uploaded.status_code == 201, uploaded.text
        image = uploaded.json()[0]
        created = client.post(
            "/agents/agent_1/runs",
            headers=headers,
            json={"input": "请根据这张截图说明预算。", "attachments": [{"id": image["id"]}]},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        bind_actor(db, owner)
        run_job(run_id, data_services=runtime_services(db))
        db.expire_all()
        job = db.get(APIJob, run_id)
        assert received
        phases = [
            body
            for body in received
            if body.get("tools") or "TurnPlanner" in json.dumps(body.get("messages", []))
        ]
        assert phases
        for body in phases:
            assert any(
                part.get("type") == "image_url"
                and part["image_url"]["url"] == "data:image/png;base64," + ENCODED
                for message in body["messages"]
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if isinstance(part, dict)
            ), body
        if reject_phase:
            assert job.status == "failed", job.result_json
            assert job.error_json["code"] == "IMAGE_INPUT_UNSUPPORTED", job.error_json
            assert "图片未被模型接受" in job.error_json["reply"]
            assert not job.retryable
        else:
            assert job.status == "succeeded", job.error_json
            assert len(phases) >= 2
            from app.core.harness_session_cleanup import harness_task_workspace_path

            frames = db.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == job.session_id
                )
            ).all()
            assert any(
                PNG == p.read_bytes()
                for frame in frames
                for p in harness_task_workspace_path(
                    tenant_id=owner.tenant_id,
                    session_id=job.session_id,
                    task_frame_id=frame.task_id,
                    db=db,
                ).glob("attachments/*-budget.png")
            )
            for loop in db.exec(select(HarnessAgentLoopRecord)).all():
                assert ENCODED not in json.dumps(loop.checkpoint_json)
        assert client.get("/runs/" + run_id, headers=_headers(token, "alice")).status_code == 404
        snapshots = db.exec(select(HarnessSessionCheckpoint).where(
            HarnessSessionCheckpoint.session_id == job.session_id,
            HarnessSessionCheckpoint.before_message_id.is_(None),
            HarnessSessionCheckpoint.status == "ready",
        )).all()
        assert any(any(message["role"] == "assistant" for message in cp.payload_json["messages"])
                   for cp in snapshots), "Even a rejected-image turn must checkpoint its final reply"
