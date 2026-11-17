"""Delegated member -> real Node engine -> local HTTP MCP gateway acceptance."""

import hashlib
import json
import re
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
    HarnessInvocationRecord,
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
    original_bytes = b"DELEGATED_ORIGINAL_BYTES_OK"
    state = {"gets": [], "bytes": original_bytes}

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
                if body["params"]["name"] == "workbench_file_original":
                    result["structuredContent"] = {"status": "SUCCEEDED", "detail": {
                        "url": f"http://127.0.0.1:{self.server.server_port}/workbench/originals/" + "a" * 43,
                        "sha256": hashlib.sha256(original_bytes).hexdigest(),
                        "size": len(original_bytes), "filename": "original.txt"}}
                if body["params"].get("arguments", {}).get("reject"):
                    result = {"isError": True, "content": [{"type": "text", "text":
                        "调用被拒绝：参数或权限不符合本次运行。"}],
                        "structuredContent": {"error": {"code": "PLATFORM_GATEWAY_REJECTED", "executed": False}}}
                if body["params"].get("arguments", {}).get("uncertain"):
                    result = {"isError": True, "content": [{"type": "text", "text": "gateway timeout"}]}
            else:
                result = {}
            encoded = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            state["gets"].append(self.path)
            allowed = self.path == "/workbench/originals/" + "a" * 43 and len(state["gets"]) == 1
            self.send_response(200 if allowed else 410)
            self.send_header("Content-Length", str(len(original_bytes) if allowed else 0))
            self.end_headers()
            if allowed:
                self.wfile.write(original_bytes)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", calls, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("original_configured", [False, True])
def test_delegated_member_gateway_original_and_denial_audit(
    db, fake_model, gateway_http, monkeypatch, original_configured
):
    model, model_base = fake_model
    gateway_url, calls, state = gateway_http
    monkeypatch.delenv("STAFFDECK_PLATFORM_ORIGINAL_BASE_URL", raising=False)
    if original_configured:
        monkeypatch.setenv("STAFFDECK_PLATFORM_ORIGINAL_BASE_URL",
                           gateway_url.removesuffix("mcp") + "workbench/originals/")
    agent, service_owner = _seed(db, model_base)
    agent.harness_max_actions = 24
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
    steps += [("mcp__staffdeck__tool_invoke", {"tool_id": "group_dispatch", "arguments": {key: True}})
              for key in ("reject", "uncertain")]
    if original_configured:
        steps += [("mcp__staffdeck__sandbox_execute", {"tool": "read_file", "arguments": {}})]
    steps += [("mcp__staffdeck__sandbox_execute", {"tool": "exec_command", "arguments": {
        "command": "/usr/bin/curl --max-time 2 -sS " + gateway_url.removesuffix("mcp") + "forbidden"}})]

    def reply(body):
        text = json.dumps(body.get("messages", []))
        if "TurnPlanner" in text and "output_contract" in text:
            return original(body)
        model.requests.append(body)
        index = len(attempted)
        if index >= len(steps):
            return [_chunk("member", {"role": "assistant", "content": "成员网关验证完成"}),
                    _chunk("member", {}, "stop")]
        if index == 7:
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
        if arguments.get("tool") == "read_file":
            match = re.search(r"attachments/originals/[a-f0-9]{32}\.txt", text)
            assert match, text
            arguments["arguments"]["path"] = match[0]
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
        assert [call["name"] for call in calls] == list(TOOL_NAMES) + ["group_dispatch"] * 2
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
        unavailable = manifests[0].payload_json["unavailable_references"]
        assert any(item["capability_id"] == "other_employee" and
                   item["unavailable_reason"] == "PLATFORM_GATEWAY_REQUIRED" for item in unavailable)
        assert not any(item["capability_id"] == "unbound_sibling" for item in unavailable)
        messages = [m for body in model.requests for m in body.get("messages", [])
                    if m.get("role") == "tool"]
        by_call = {m.get("tool_call_id"): str(m.get("content")) for m in messages}
        assert all(name in by_call["member-0"] for name in TOOL_NAMES)
        for index in (1, 3, 4):
            assert "MEMBER_GATEWAY_OK" in by_call[f"member-{index}"]
        assert ("workspace_path" in by_call["member-2"]) is original_configured
        assert "a" * 43 not in str(by_call)
        assert state["gets"] == (["/workbench/originals/" + "a" * 43] if original_configured else [])
        network_attempt = by_call[f"member-{len(steps) - 1}"]
        assert "SANDBOX_UNAVAILABLE" not in network_attempt
        assert "exit_code" in network_attempt
        assert "forbidden" not in state["gets"]
        if original_configured:
            assert "DELEGATED_ORIGINAL_BYTES_OK" in by_call["member-10"]
        assert "PLATFORM_GATEWAY_REJECTED" in by_call["member-8"]
        assert "outcome_unknown" in by_call["member-9"]
        for index in (5, 6, 7):
            assert "MEMBER_GATEWAY_OK" not in by_call[f"member-{index}"]
            assert "PERMISSION_DENIED" in by_call[f"member-{index}"]
        result = client.get(f"/runs/{run_id}/result", headers=_headers(token, "bob"))
        assert result.status_code == 200
        audit = result.json()["tool_calls"]
        assert sum(call["status"] == "denied" for call in audit) >= 3
        rejected = [call for call in audit if (call["result"].get("error") or {}).get("code") == "PLATFORM_GATEWAY_REJECTED"]
        assert len(rejected) == 1 and rejected[0]["status"] == "failed"
        assert "a" * 43 not in result.text
        rows = db.exec(select(HarnessInvocationRecord).where(HarnessInvocationRecord.session_id == session.id)).all()
        assert "a" * 43 not in str([(row.result_json, row.response_cache_json) for row in rows])
        assert client.get(f"/runs/{run_id}/result", headers=_headers(token, "alice")).status_code == 404
