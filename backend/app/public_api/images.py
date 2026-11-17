"""Owner- and employee-bound image references for the public runtime API."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.db.models import new_id
from app.public_api.errors import PublicAPIError
from app.session.attachment_store import (
    _atomic_write,
    _attachment_directory,
    read_staged_chat_attachment,
    stage_chat_attachment,
)
from app.session.attachments import parse_chat_attachment, validate_chat_turn_attachments
from app.session.session_schema import ChatAttachmentRead

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_RUN_IMAGES = 8
IMAGE_TTL_DAYS = 7
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


def _validate_image(image: ChatAttachmentRead) -> ChatAttachmentRead:
    if image.content_type not in IMAGE_TYPES or image.kind != "image":
        raise PublicAPIError(415, "IMAGE_TYPE_UNSUPPORTED", "只支持 PNG、JPEG 和 WebP 图片。")
    if image.size > MAX_IMAGE_BYTES:
        raise PublicAPIError(413, "IMAGE_TOO_LARGE", "每张图片最多 4 MiB。")
    try:
        result = validate_chat_turn_attachments(
            [image], max_attachments=1, max_attachment_bytes=MAX_IMAGE_BYTES
        )[0]
    except ValueError as exc:
        raise PublicAPIError(
            400, "IMAGE_ATTACHMENT_INVALID", "图片内容无效或与上传记录不一致。"
        ) from exc
    if not result.data_url:
        raise PublicAPIError(400, "IMAGE_ATTACHMENT_INVALID", "图片没有可供模型读取的有效内容。")
    return result


def stage_public_image(
    data: bytes, filename: str, content_type: str, *, principal, agent_id: str
) -> dict:
    if len(data) > MAX_IMAGE_BYTES:
        raise PublicAPIError(413, "IMAGE_TOO_LARGE", "每张图片最多 4 MiB。")
    if content_type not in IMAGE_TYPES:
        raise PublicAPIError(415, "IMAGE_TYPE_UNSUPPORTED", "只支持 PNG、JPEG 和 WebP 图片。")
    image = _validate_image(parse_chat_attachment(filename, content_type, data, extract_text=False))
    if image.content_type != content_type:
        raise PublicAPIError(400, "IMAGE_ATTACHMENT_INVALID", "图片文件名与 MIME 类型不一致。")
    image = image.model_copy(update={"id": new_id("pimg")})
    tenant, user = principal.tenant_id, principal.actor_user.id
    image = stage_chat_attachment(image, data, tenant_id=tenant, user_id=user)
    directory = _attachment_directory(tenant_id=tenant, user_id=user, attachment_id=image.id)
    expires = datetime.now(UTC) + timedelta(days=IMAGE_TTL_DAYS)
    metadata = {
        "tenant_id": tenant,
        "user_id": user,
        "agent_id": agent_id,
        "expires_at": expires.isoformat(),
        "attachment": image.model_dump(mode="json", exclude={"data_url", "text"}),
    }
    try:
        _atomic_write(directory / "public-image.json", json.dumps(metadata).encode())
    except OSError:
        shutil.rmtree(directory)
        raise
    return {
        "id": image.id,
        "kind": "image",
        "filename": image.filename,
        "content_type": image.content_type,
        "size": image.size,
        "sha256": image.sha256,
        "expires_at": expires.isoformat(),
    }


def _load_image(reference: str, *, principal, agent_id: str) -> ChatAttachmentRead:
    directory = _attachment_directory(
        tenant_id=principal.tenant_id, user_id=principal.actor_user.id, attachment_id=reference
    )
    record = directory / "public-image.json"
    try:
        if record.is_symlink():
            raise ValueError("unsafe reference")
        metadata = json.loads(record.read_text())
        if (
            metadata.get("tenant_id") != principal.tenant_id
            or metadata.get("user_id") != principal.actor_user.id
            or metadata.get("agent_id") != agent_id
        ):
            raise ValueError("reference owner mismatch")
        image = ChatAttachmentRead.model_validate(metadata["attachment"])
        expires = datetime.fromisoformat(metadata["expires_at"])
        if expires.tzinfo is None:
            raise ValueError("invalid expiry")
        if image.id != reference:
            raise ValueError("reference identity mismatch")
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        raise PublicAPIError(
            404, "IMAGE_ATTACHMENT_NOT_FOUND", "图片引用不存在或不属于当前用户和员工。"
        ) from exc
    if expires <= datetime.now(UTC):
        shutil.rmtree(directory)
        raise PublicAPIError(410, "IMAGE_ATTACHMENT_EXPIRED", "图片引用已过期，请重新上传。")
    try:
        data = read_staged_chat_attachment(
            image, tenant_id=principal.tenant_id, user_id=principal.actor_user.id
        )
    except (ValueError, TypeError):
        data = None
    if data is None:
        raise PublicAPIError(409, "IMAGE_ATTACHMENT_INVALID", "图片暂存已损坏，请重新上传。")
    parsed = parse_chat_attachment(image.filename, image.content_type, data, extract_text=False)
    return _validate_image(
        parsed.model_copy(
            update={"id": image.id, "sha256": image.sha256, "sandbox_path": image.sandbox_path}
        )
    )


def discard_public_image(reference: str, *, principal) -> None:
    """Rollback a newly staged reference when its multipart batch fails."""
    directory = _attachment_directory(
        tenant_id=principal.tenant_id, user_id=principal.actor_user.id, attachment_id=reference
    )
    shutil.rmtree(directory, ignore_errors=True)


def resolve_run_attachments(
    raw: list[dict[str, Any]], *, principal, agent_id: str
) -> list[ChatAttachmentRead]:
    if len(raw) > MAX_RUN_IMAGES:
        raise PublicAPIError(400, "TOO_MANY_ATTACHMENTS", "每次运行最多携带 8 个附件。")
    attachments = []
    for item in raw:
        reference = item.get("id")
        if isinstance(reference, str) and reference.startswith("pimg_"):
            if set(item) != {"id"}:
                raise PublicAPIError(
                    400, "IMAGE_REFERENCE_INVALID", "图片引用只需 id，不接受覆盖内容或路径。"
                )
            attachments.append(_load_image(reference, principal=principal, agent_id=agent_id))
            continue
        try:
            image = ChatAttachmentRead.model_validate(item)
        except ValidationError as exc:
            raise PublicAPIError(
                400, "ATTACHMENT_INVALID", "附件无效，请先上传后使用返回的引用。"
            ) from exc
        if image.kind == "image":
            # Compatibility for the earlier owner-scoped multipart endpoint: uploaded
            # bytes remain authoritative, never a caller's data URL or host path.
            image = _validate_image(image)
            data = read_staged_chat_attachment(
                image, tenant_id=principal.tenant_id, user_id=principal.actor_user.id
            )
            if data is None:
                raise PublicAPIError(
                    404, "IMAGE_ATTACHMENT_NOT_FOUND", "请先使用当前用户身份上传图片。"
                )
        attachments.append(image)
    return attachments


def cleanup_expired_public_images(*, now: datetime | None = None) -> int:
    """Maintenance command: remove expired staged public uploads, not workspace copies."""
    from app import paths

    root = paths.user_data_dir().resolve() / "harness_uploads"
    if root.is_symlink():
        raise ValueError("unsafe upload root")
    count = 0
    for record in root.glob("*/*/public-image.json"):
        if record.is_symlink() or record.parent.is_symlink() or record.parent.parent.is_symlink():
            continue
        try:
            metadata = json.loads(record.read_text())
            expires = datetime.fromisoformat(metadata["expires_at"])
            if expires <= (now or datetime.now(UTC)):
                shutil.rmtree(record.parent)
                count += 1
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return count


if __name__ == "__main__":
    print(json.dumps({"expired_image_uploads_removed": cleanup_expired_public_images()}))
