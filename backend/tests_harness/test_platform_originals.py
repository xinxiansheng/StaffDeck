"""Exact endpoint allowlist, safe original import and denial/cleanup regressions."""
import hashlib
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from staffdeck_harness.capabilities.platform_originals import CONFIG, import_original
from staffdeck_harness.contracts.errors import PermissionDenied

TOKEN = "a" * 43
CONTENT = b"synthetic original bytes\x00\xff"


@pytest.fixture
def originals():
    state = {"gets": [], "status": 200, "body": CONTENT}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["gets"].append(self.path)
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.send_header("Location", "/forbidden")
            self.end_headers()
            self.wfile.write(state["body"])
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/workbench/originals/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def payload(base, **updates):
    return {"status": "SUCCEEDED", "detail": {"url": base + TOKEN,
        "sha256": hashlib.sha256(CONTENT).hexdigest(), "size": len(CONTENT),
        "filename": "original.docx", **updates}}


@pytest.mark.parametrize("configured", [False, True])
def test_default_deny_and_configured_exact_download(originals, monkeypatch, tmp_path, configured, caplog):
    base, state = originals
    monkeypatch.delenv(CONFIG, raising=False)
    if configured:
        monkeypatch.setenv(CONFIG, base)
    checks = []
    caplog.set_level(logging.INFO, logger="httpx")
    result = import_original(payload(base), tmp_path, lambda: checks.append(True))
    assert result.success is configured
    if configured:
        detail = result.data["detail"]
        assert (tmp_path / detail["workspace_path"]).read_bytes() == CONTENT
        assert detail["sha256"] == hashlib.sha256(CONTENT).hexdigest()
        assert state["gets"] == ["/workbench/originals/" + TOKEN]
        assert checks == [True, True]
    else:
        assert not state["gets"] and not list(tmp_path.rglob("*"))
    assert TOKEN not in str(result)
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("variant", ["host", "port", "scheme", "prefix", "query", "fragment",
    "userinfo", "traversal", "encoded", "backslash", "token", "controls", "suffix"])
def test_other_addresses_never_contacted(originals, monkeypatch, tmp_path, variant):
    base, state = originals
    monkeypatch.setenv(CONFIG, base)
    valid = base + TOKEN
    urls = {"host": valid.replace("127.0.0.1", "localhost"),
        "port": "http://127.0.0.1:1/workbench/originals/" + TOKEN,
        "scheme": valid.replace("http:", "https:"),
        "prefix": valid.replace("/originals/", "/admin/"), "query": valid + "?x=1",
        "fragment": valid + "#x", "userinfo": valid.replace("//", "//user@", 1),
        "traversal": base + "../" + TOKEN, "encoded": base + "%61" + TOKEN[1:],
        "backslash": base + "\\" + TOKEN, "token": base + "short",
        "controls": valid + "\x00", "suffix": valid + "/extra"}
    assert not import_original(payload(base, url=urls[variant]), tmp_path, lambda: None).success
    assert not state["gets"]


@pytest.mark.parametrize("suffix", ["/", "/admin/", "/../workbench/originals/", "/workbench/originals/?x="])
def test_bad_configuration_fails_closed(originals, monkeypatch, tmp_path, suffix):
    base, state = originals
    bad = base.split("/workbench")[0] + suffix
    monkeypatch.setenv(CONFIG, bad)
    assert not import_original(payload(bad), tmp_path, lambda: None).success
    assert not state["gets"]


@pytest.mark.parametrize("userinfo", ["@", "user:password@"])
def test_configuration_cannot_embed_userinfo(originals, monkeypatch, tmp_path, userinfo):
    base, state = originals
    bad = base.replace("http://", "http://" + userinfo)
    monkeypatch.setenv(CONFIG, bad)
    assert not import_original(payload(bad), tmp_path, lambda: None).success
    assert not state["gets"]


@pytest.mark.parametrize("status", [301, 302, 307, 409, 410, 500])
def test_no_redirect_retry_or_partial_file(originals, monkeypatch, tmp_path, status):
    base, state = originals
    monkeypatch.setenv(CONFIG, base)
    state["status"] = status
    result = import_original(payload(base), tmp_path, lambda: None)
    assert not result.success and len(state["gets"]) == 1
    assert not list(tmp_path.rglob("*.docx")) and TOKEN not in str(result)


@pytest.mark.parametrize("damage", ["hash", "short", "long", "oversize", "bool-size"])
def test_integrity_and_size_fail_closed(originals, monkeypatch, tmp_path, damage):
    base, state = originals
    monkeypatch.setenv(CONFIG, base)
    data = payload(base)
    if damage == "hash":
        data["detail"]["sha256"] = "0" * 64
    elif damage == "short":
        state["body"] = CONTENT[:-1]
    elif damage == "long":
        state["body"] = CONTENT + b"extra"
    else:
        data["detail"]["size"] = True if damage == "bool-size" else 12 * 1024 * 1024 + 1
    assert not import_original(data, tmp_path, lambda: None).success
    assert not list(tmp_path.rglob("*.docx"))
    assert len(state["gets"]) == (0 if damage in {"oversize", "bool-size"} else 1)


@pytest.mark.parametrize("level", ["attachments", "originals"])
def test_workspace_symlinks_are_not_followed(originals, monkeypatch, tmp_path, level):
    base, state = originals
    monkeypatch.setenv(CONFIG, base)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = workspace / "attachments"
    if level == "originals":
        link.mkdir()
        link = link / "originals"
    link.symlink_to(outside, target_is_directory=True)
    assert not import_original(payload(base), workspace, lambda: None).success
    assert not state["gets"] and not list(outside.iterdir())


def test_revocation_during_download_removes_file(originals, monkeypatch, tmp_path):
    base, state = originals
    monkeypatch.setenv(CONFIG, base)
    def check():
        if state["gets"]:
            raise PermissionDenied("revoked")
    with pytest.raises(PermissionDenied):
        import_original(payload(base), tmp_path, check)
    assert not list(tmp_path.rglob("*.docx"))


@pytest.mark.parametrize("error,typed", [
    ({"code": "PLATFORM_GATEWAY_REJECTED", "executed": False}, True),
    ({"code": "PLATFORM_GATEWAY_REJECTED", "executed": True}, False),
    ({"code": "PLATFORM_GATEWAY_REJECTED", "executed": 0}, False),
    ({"code": "PLATFORM_GATEWAY_REJECTED"}, False),
    ({"code": "TIMEOUT", "executed": False}, False),
])
def test_mcp_not_executed_requires_explicit_structured_contract(error, typed):
    from app.tools.mcp_client import MCPClientError, MCPToolNotExecutedError, _tool_result_envelope
    with pytest.raises(MCPClientError) as failure:
        _tool_result_envelope({"isError": True, "structuredContent": {"error": error},
                              "content": [{"type": "text", "text": "调用被拒绝：参数或权限不符合本次运行。"}]})
    assert isinstance(failure.value, MCPToolNotExecutedError) is typed
