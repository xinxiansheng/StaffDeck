"""An employee-bound MCP tool authorizes its transport, not the whole server."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlmodel import Session, select

from app.agents.branching import ensure_private_resource_binding
from app.db.models import AgentProfile, AgentResourceBinding, MCPServer, Tool, User
from staffdeck_harness.capabilities.facade import FacadeDeps, ToolFacade
from staffdeck_harness.composition.local_sources import LocalResourceCatalog
from staffdeck_harness.composition.projection import live_resource_ref
from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation
from staffdeck_harness.contracts.security import SecurityContext
from staffdeck_harness.contracts.sources import SourceContext
from staffdeck_harness.security.oss_local import build_oss_local_profile
from staffdeck_harness.security.profile import Guard
from tests_harness.test_capability_host_hardening import db as hardening_db

db = hardening_db


@pytest.fixture
def gateway(db, monkeypatch):
    db.add(User(id="shadow", tenant_id="t1", username="shadow", source="platform",
                role="member", password_hash="!"))
    server = MCPServer(id="gateway", tenant_id="t1", name="platform_employee1",
                       transport="streamable_http", url="http://gateway.invalid/mcp")
    tool = Tool(id="bound", tenant_id="t1", name="web_search", tool_type="mcp",
                method="POST", url="", mcp_server_id=server.id,
                config_json={"tool_name": "web_search"})
    db.add(server)
    db.add(tool)
    ensure_private_resource_binding(db, "t1", "a1", "tool", tool.id)
    db.commit()
    sends = []

    def transport(config, arguments, **kwargs):
        sends.append((config["url"], arguments, kwargs["tool_name"]))
        return {"marker": "GATEWAY_OK"}

    # Only the network is replaced; use the real catalog, PEP, facade and ToolExecutor.
    monkeypatch.setattr("app.tools.tool_executor.execute_mcp_tool", transport)
    guard = Guard("test.gateway", build_oss_local_profile())
    identity = SecurityContext("shadow", "t1", tenant_role="member")
    context = SourceContext("t1", "a1", user_id="shadow")
    inv = ModuleInvocation("call", "tool", "tool.invoke/v1", {"tool_id": tool.id},
        InvocationContext("t1", "a1", "shadow", "s1", "turn", "web"), binding_id=tool.id)
    facade = ToolFacade(FacadeDeps(db, guard, identity, agent_row=db.get(AgentProfile, "a1")))
    return server, tool, guard, identity, context, inv, facade, sends


def _authorize(db, gateway):
    _, tool, guard, identity, context, _, _, _ = gateway
    descriptor = LocalResourceCatalog(db).resolve(context, "tool", tool.id, "tool.invoke/v1")
    if not descriptor.available:
        raise ModuleSdkError("unavailable", code="RESOURCE_UNAVAILABLE")
    for ref in (descriptor.ref, *descriptor.related_resources):
        guard.require(identity, descriptor.operation, ref)
    return descriptor


@pytest.mark.parametrize("name", [
    "web_search", "workbench_file_original", "group_dispatch", "legal_gateway",
])
def test_member_can_use_bound_mcp_tool_without_server_binding(db, gateway, name):
    server, tool, guard, identity, _, inv, facade, sends = gateway
    tool.name = name
    tool.config_json = {"tool_name": name}
    db.add(tool)
    db.commit()
    before = list(db.exec(select(AgentResourceBinding)).all())
    assert len(before) == 1 and before[0].resource_type == "tool"
    assert identity.tenant_role == "member"
    _authorize(db, gateway)
    result = facade.invoke(inv)
    assert result.success, result.error
    assert sends == [(server.url, {}, name)]
    assert len(db.exec(select(AgentResourceBinding)).all()) == 1
    # Inheritance exists only on this tool dependency; direct server use/manage still fails.
    direct = live_resource_ref(db, "t1", "mcp_server", server, agent=db.get(AgentProfile, "a1"))
    assert not guard.profile.pep.authorize(identity, "mcp", "use", direct).allowed
    assert not guard.profile.pep.authorize(identity, "mcp", "manage", direct).allowed


@pytest.mark.parametrize("change", [
    "tool_binding_disabled", "tool_binding_deleted", "tool_binding_removed",
    "other_employee_binding", "non_private_binding", "tool_disabled",
    "server_disabled", "server_deleted", "foreign_server",
    "server_binding_disabled", "server_binding_deleted",
])
def test_bound_tool_inheritance_never_overrides_revocation(db, gateway, change):
    server, tool, _, _, _, inv, facade, sends = gateway
    # Keep references cached while another transaction revokes the live authority.
    binding = db.exec(select(AgentResourceBinding)).one()
    with Session(db.get_bind()) as writer:
        if change.startswith("server_binding_"):
            writer.add(AgentResourceBinding(tenant_id="t1", agent_id="a1",
                resource_type="mcp_server", resource_id=server.id,
                status=change.removeprefix("server_binding_"),
                metadata_json={"scope": "agent_private"}))
        elif change in {"tool_disabled", "server_disabled", "server_deleted", "foreign_server"}:
            row = writer.get(Tool if change == "tool_disabled" else MCPServer,
                             tool.id if change == "tool_disabled" else server.id)
            if change == "server_deleted":
                writer.delete(row)
            else:
                if change == "foreign_server":
                    row.tenant_id = "other-tenant"
                else:
                    row.enabled = False
                writer.add(row)
        else:
            row = writer.get(AgentResourceBinding, binding.id)
            if change == "tool_binding_removed":
                writer.delete(row)
            else:
                if change == "other_employee_binding":
                    row.agent_id = "other-agent"
                elif change == "non_private_binding":
                    row.metadata_json = {}
                else:
                    row.status = change.removeprefix("tool_binding_")
                writer.add(row)
        writer.commit()
    with pytest.raises(ModuleSdkError):
        _authorize(db, gateway)
    try:
        result = facade.invoke(inv)
    except ModuleSdkError:
        pass
    else:
        assert not result.success
    assert sends == []


def test_tool_dependency_does_not_authorize_unbound_sibling(db, gateway):
    server, _, _, _, _, inv, facade, sends = gateway
    sibling = Tool(id="sibling", tenant_id="t1", name="unbound_sibling", tool_type="mcp",
                   method="POST", url="", mcp_server_id=server.id)
    db.add(sibling)
    db.commit()
    other = (server, sibling, *gateway[2:])
    with pytest.raises(ModuleSdkError):
        _authorize(db, other)
    with pytest.raises(ModuleSdkError):
        facade.invoke(replace(inv, binding_id=sibling.id, arguments={"tool_id": sibling.id}))
    assert sends == []
