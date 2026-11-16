"""Delegated member -> real Node engine -> local HTTP MCP gateway acceptance."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.agents.branching import ensure_private_resource_binding
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentResourceBinding,
    APIClient,
    APICredential,
    APIJob,
    ChatSession,
    MCPServer,
    Tool,
    User,
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
TOOL_NAMES = ("web_search", "workbench_file_original", "group_dispatch", "legal_gateway")


@pytest.fixture
def gateway_http():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = body.get("method")
            if "id" not in body:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": body["params"]["protocolVersion"],
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "synthetic-platform-gateway", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                                    for name in TOOL_NAMES]}
            elif method == "tools/call":
                calls.append(body["params"])
                result = {"content": [{"type": "text", "text": json.dumps({
                    "marker": "MEMBER_GATEWAY_OK", "tool": body["params"]["name"],
                })}], "isError": False}
            else:
                result = {}
            encoded = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_delegated_member_gateway_tools_work_without_server_binding(
    db, fake_model, gateway_http, monkeypatch
):
    model, model_base = fake_model
    gateway_url, calls = gateway_http
    agent, service_owner = _seed(db, model_base)
    agent.harness_max_actions = 16
    service_owner.role = "admin"
    db.add(agent)
    db.add(service_owner)
    for server_id, name in (("gateway", "platform_employee1"),
                            ("other-gateway", "platform_employee2")):
        db.add(MCPServer(id=server_id, tenant_id="tenant_demo", name=name,
                         transport="streamable_http", url=gateway_url))
    for name in (*TOOL_NAMES, "unbound_sibling", "other_employee"):
        db.add(Tool(id=name, tenant_id="tenant_demo", name=name, tool_type="mcp",
                    method="POST", url="", input_schema={"type": "object"},
                    mcp_server_id="other-gateway" if name == "other_employee" else "gateway",
                    config_json={"tool_name": name}))
        if name != "unbound_sibling":
            ensure_private_resource_binding(db, "tenant_demo", agent.id, "tool", name)
    token, prefix, digest = generate_api_key()
    scopes = ["*", "platform:delegate"]
    db.add(APIClient(id="platform", tenant_id="tenant_demo", name="Synthetic platform",
        created_by_user_id=service_owner.id, scopes_json=scopes,
        metadata_json={"platform_tenant_id": "platform_tenant"}))
    db.add(APICredential(id="platform-key", tenant_id="tenant_demo", client_id="platform",
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
    original = model.reply
    attempted = []
    steps = [("mcp__staffdeck__capability_describe", {"kind": "tool"})]
    steps += [("mcp__staffdeck__tool_invoke", {"tool_id": name, "arguments": {}})
              for name in (*TOOL_NAMES, "unbound_sibling", "other_employee", "web_search")]

    def reply(body):
        text = json.dumps(body.get("messages", []))
        if "TurnPlanner" in text and "output_contract" in text:
            return original(body)
        model.requests.append(body)
        index = len(attempted)
        if index >= len(steps):
            return [_chunk("member", {"role": "assistant", "content": "成员网关验证完成"}),
                    _chunk("member", {}, "stop")]
        if index == len(steps) - 1:
            with Session(engine) as writer:
                binding = writer.exec(select(AgentResourceBinding).where(
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "tool",
                    AgentResourceBinding.resource_id == "web_search",
                )).one()
                binding.status = "disabled"
                writer.add(binding)
                writer.commit()
        name, arguments = steps[index]
        attempted.append(name)
        return [_chunk("member", {"role": "assistant", "tool_calls": [{
            "index": 0, "id": f"member-{index}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}), _chunk("member", {}, "tool_calls")]

    monkeypatch.setattr(model, "reply", reply)
    with TestClient(app) as client:
        response = client.post("/agents/agent_1/runs", headers=_headers(token, "bob"),
                               json={"input": "检查员工已授权的平台网关工具"})
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        bind_actor(db, service_owner)
        run_job(run_id, data_services=runtime_services(db))
        db.expire_all()
        job = db.get(APIJob, run_id)
        assert job.status == "succeeded", job.error_json
        session = db.get(ChatSession, job.session_id)
        shadow = db.get(User, session.user_id)
        assert shadow.source == "platform" and shadow.role == "member"
        assert shadow.id != service_owner.id
        assert len(attempted) == len(steps)
        assert [call["name"] for call in calls] == list(TOOL_NAMES)
        assert db.exec(select(AgentResourceBinding).where(
            AgentResourceBinding.resource_type == "mcp_server",
        )).all() == []
        # The manifest and discovery both expose the bound tools before execution.
        manifests = db.exec(select(AgentEvent).where(
            AgentEvent.session_id == session.id,
            AgentEvent.event_type == "capability_manifest_resolved",
        )).all()
        assert manifests
        assert set(TOOL_NAMES) <= set(manifests[0].payload_json["trace_names"]["tools"])
        messages = [m for body in model.requests for m in body.get("messages", [])
                    if m.get("role") == "tool"]
        by_call = {m.get("tool_call_id"): str(m.get("content")) for m in messages}
        assert all(name in by_call["member-0"] for name in TOOL_NAMES)
        for index in range(1, 5):
            assert "MEMBER_GATEWAY_OK" in by_call[f"member-{index}"]
        for index in (5, 6, 7):
            assert "MEMBER_GATEWAY_OK" not in by_call[f"member-{index}"]
            assert "PERMISSION_DENIED" in by_call[f"member-{index}"]
        assert client.get(f"/runs/{run_id}/result", headers=_headers(token, "bob")).status_code == 200
        assert client.get(f"/runs/{run_id}/result", headers=_headers(token, "alice")).status_code == 404
