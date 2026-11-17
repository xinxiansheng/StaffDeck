"""Import a platform original without giving the delegated sandbox network access.

The URL is an untrusted tool result. Only an operator-configured, exact endpoint
may be fetched; neither the model nor the tool controls the destination path.
"""

import hashlib
import logging
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from staffdeck_harness.contracts.invocation import ModuleResult

CONFIG = "STAFFDECK_PLATFORM_ORIGINAL_BASE_URL"
MAX_BYTES = 12 * 1024 * 1024
DESCRIPTION = (
    "获取当前运行有权访问的平台原件。StaffDeck 服务端校验大小及 SHA-256 后放入当前运行工作区，"
    "返回 workspace_path；请直接用工作区文件工具读取该路径，无需 shell 下载或访问网络。"
)


class _HideOriginalURL(logging.Filter):
    def filter(self, record):
        # httpx INFO normally logs the complete request URL, including its token.
        message = record.getMessage()
        if "/workbench/originals/" in message:
            record.msg = re.sub(r"https?://[^\s]+/workbench/originals/[^\s]+",
                                "<platform-original-url-redacted>", message)
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_HideOriginalURL())


class OriginalImportError(Exception):
    pass


def _fail(message):
    raise OriginalImportError(message)


def validated_url(url):
    base = os.environ.get(CONFIG, "")
    if not base:
        _fail("未配置平台原件下载地址，保持禁网。")
    # Reject ambiguous encodings before URL parsing (which strips some controls).
    for value in (base, url):
        if not isinstance(value, str) or re.search(r"[\s\x00-\x20\x7f\\%?#]", value):
            _fail("平台原件地址不符合配置规则。")
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and parsed.port != 0 and parsed.path.endswith("/workbench/originals/")
                 and all(p not in {".", ".."} for p in parsed.path.split("/"))
                 and "//" not in parsed.path)
        _ = urlsplit(url).port  # reject malformed authority
    except ValueError:
        valid = False
    if not valid or not url.startswith(base) or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", url[len(base):]
    ):
        _fail("平台原件地址不在部署允许的完整地址范围内。")
    return url


def import_original(data, workspace: Path, reauthorize, *, remaining_seconds=None):
    """Return only verified local metadata; never persist/expose the one-use URL."""
    parent_fd = None
    filename = None
    complete = False
    created = False
    try:
        detail = data.get("detail") if isinstance(data, dict) else None
        if not isinstance(detail, dict) or data.get("status") != "SUCCEEDED":
            _fail("平台未返回成功的原件下载信息。")
        url = validated_url(detail.get("url"))
        size, digest = detail.get("size"), detail.get("sha256")
        if type(size) is not int or not 0 < size <= MAX_BYTES or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            _fail("平台原件缺少有效的大小或 SHA-256，未下载。")
        original_name = detail.get("filename")
        if not isinstance(original_name, str) or len(original_name) > 255:
            _fail("平台原件文件名无效。")
        # The filename is display metadata only, never a path supplied to the OS.
        suffix = Path(original_name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ".bin"
        filename = uuid.uuid4().hex + suffix
        reauthorize()
        budget = min(30.0, remaining_seconds if remaining_seconds is not None else 30.0)
        if budget <= 0:
            _fail("本次运行已超时，未下载原件。")
        deadline = time.monotonic() + budget
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(workspace, flags)
        for name in ("attachments", "originals"):
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(name, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child_fd
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        created = True
        actual_size, hasher = 0, hashlib.sha256()
        with os.fdopen(fd, "wb") as output, httpx.Client(
            trust_env=False, follow_redirects=False, timeout=budget
        ) as client, client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                if response.status_code in {409, 410}:
                    _fail(f"平台原件链接已失效或运行已结束（{response.status_code}），请重新获取。")
                _fail("平台原件下载被拒绝；不会跟随跳转或自动重试。")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                _fail("平台原件必须以未经传输压缩的原始字节返回。")
            for chunk in response.iter_raw():
                actual_size += len(chunk)
                if actual_size > size or time.monotonic() > deadline:
                    _fail("平台原件下载超过声明大小或时间限制。")
                hasher.update(chunk)
                output.write(chunk)
        if actual_size != size or hasher.hexdigest() != digest.lower():
            _fail("平台原件大小或 SHA-256 校验失败，文件未交付。")
        reauthorize()
        complete = True
        return ModuleResult.ok({"status": "SUCCEEDED", "detail": {
            "workspace_path": "attachments/originals/" + filename,
            "filename": original_name, "size": actual_size, "sha256": hasher.hexdigest(),
            "download_status": "ready", "instruction": "原件已进入当前运行工作区，请直接读取 workspace_path。",
        }})
    except OriginalImportError as exc:
        return ModuleResult.fail("PLATFORM_ORIGINAL_UNAVAILABLE", str(exc))
    except (httpx.HTTPError, OSError, ValueError):
        # Exception text can contain the bearer URL. Never return or log it.
        return ModuleResult.fail("PLATFORM_ORIGINAL_UNAVAILABLE", "原件下载或安全落盘失败，文件未交付。")
    finally:
        if parent_fd is not None:
            if created and not complete:
                try:
                    os.unlink(filename, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)
