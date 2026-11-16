"""Platform runs use their owner's workspace and the platform's live tool gateway.

Names alone are not authority: the tool must still belong to the employee's MCP
server. Native business tools/knowledge/custom operations cannot bypass that gateway.
"""

import re
from dataclasses import replace

from app.db.models import MCPServer, Tool, User
from staffdeck_harness.contracts.errors import PermissionDenied


def _gateway_name(employee_id: str) -> str:
    suffix = re.sub(r"[^a-z0-9]", "", employee_id.lower())[:12]
    if not suffix:
        raise PermissionDenied("平台员工身份无效")
    return "platform_" + suffix


def _allowed_tool(db, tenant_id, tool_id, server_name, allowed):
    # Refresh live rows even when the model has been thinking for a long time.
    tool = db.get(Tool, tool_id, populate_existing=True)
    if (
        tool is None
        or tool.tenant_id != tenant_id
        or not tool.enabled
        or tool.tool_type != "mcp"
        or not tool.mcp_server_id
    ):
        return False
    server = db.get(MCPServer, tool.mcp_server_id, populate_existing=True)
    if (
        server is None
        or server.tenant_id != tenant_id
        or server.name != server_name
        or not server.enabled
    ):
        return False
    return allowed is None or tool.id in allowed or tool.name in allowed


def restrict_snapshot(db, snapshot, request, session):
    actor = db.get(User, request.user_id) if request.user_id else None
    subject = (session.channel_target_json or {}).get("_platform_subject")
    if not subject and (actor is None or actor.source != "platform"):
        return snapshot
    if (
        not subject
        or actor is None
        or actor.source != "platform"
        or actor.role != "member"
        or actor.tenant_id != session.tenant_id
        or actor.id != session.user_id
        or subject.get("agent_id") != session.agent_id
    ):
        raise PermissionDenied("平台执行身份与私有会话不一致")
    server_name = _gateway_name(subject["employee_id"])
    allowed = request._platform_tools
    return replace(
        snapshot,
        session_policy={
            **snapshot.session_policy,
            "platform_gateway_server": server_name,
            "platform_allowed_tools": allowed,
            "platform_subject": subject,
            "platform_credential": (
                db.info.get("staffdeck_platform_principal").credential_id
                if db.info.get("staffdeck_platform_principal")
                else (session.channel_target_json or {}).get("_platform_credential")
            ),
            "sandbox_enabled": True,
            "sandbox_network_mode": "deny",
            "sandbox_allowed_domains": [],
        },
        grants=tuple(
            g
            for g in snapshot.grants
            if g.operation == "tool.invoke/v1"
            and _allowed_tool(db, session.tenant_id, g.resource_id, server_name, allowed)
        ),
    )


def require_invocation(db, snapshot, invocation):
    policy = snapshot.session_policy
    server_name = policy.get("platform_gateway_server")
    if not server_name:
        return
    from app.public_api.auth import principal_for_credential
    from app.public_api.errors import PublicAPIError

    try:
        principal = principal_for_credential(
            db, policy.get("platform_credential"), subject=policy.get("platform_subject")
        )
    except PublicAPIError as exc:
        raise PermissionDenied("平台授权已失效，停止工具调用") from exc
    if (
        principal.actor_user.id != invocation.context.user_id
        or principal.tenant_id != invocation.context.tenant_id
        or principal.agent_id != invocation.context.agent_id
        or not principal.can("runs:create")
    ):
        raise PermissionDenied("工具调用人与平台会话不一致")
    if invocation.operation == "sandbox.execute/v1":
        # The workspace facade validates paths/symlinks. Commands require the OS sandbox;
        # an unavailable sandbox fails closed, with no unsandboxed fallback or network.
        return
    if (
        invocation.operation == "tool.invoke/v1"
        and invocation.arguments.get("tool_id", invocation.binding_id) == invocation.binding_id
        and _allowed_tool(
            db,
            snapshot.tenant_id,
            invocation.binding_id,
            server_name,
            policy.get("platform_allowed_tools"),
        )
    ):
        return
    raise PermissionDenied("平台运行只能通过当前员工的授权网关访问业务资源")
