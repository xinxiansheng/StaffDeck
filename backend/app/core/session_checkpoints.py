"""Private, bounded turn snapshots. No replay of effects, jobs, credentials or memories."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath

from sqlmodel import Session, select

from app.core.harness_session_cleanup import (
    harness_path_segment,
    harness_session_workspace_path,
)
from app.db.models import (
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessSessionCheckpoint,
    HarnessTaskFrameRecord,
    Message,
    new_id,
    utc_now,
)
from app.harness.artifacts import HarnessArtifactAccessError, open_harness_artifact

RETENTION_DAYS = 30
MAX_CHECKPOINTS = 50
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SESSION_BYTES = 512 * 1024 * 1024
MAX_FILES = 2000
MAX_STATE_BYTES = 8 * 1024 * 1024


class CheckpointUnavailable(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _rows(db, model, session):
    return db.exec(
        select(model).where(
            model.tenant_id == session.tenant_id,
            model.session_id == session.id,
        )
    ).all()


def _state(db: Session, session: ChatSession) -> dict:
    messages = sorted(_rows(db, Message, session), key=lambda row: row.created_at)
    frames = _rows(db, HarnessTaskFrameRecord, session)
    if any(row.status == "running" for row in frames):
        raise CheckpointUnavailable("FORK_CHECKPOINT_UNSTABLE", "任务尚未到达稳定检查点。")
    return {
        "version": 1,
        "session": session.model_dump(mode="json"),
        "messages": [row.model_dump(mode="json") for row in messages],
        "frames": [row.model_dump(mode="json") for row in sorted(frames, key=lambda row: row.id)],
        "loops": [
            row.model_dump(mode="json")
            for row in sorted(_rows(db, HarnessAgentLoopRecord, session), key=lambda row: row.id)
        ],
    }


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(payload: dict, archive: bytes) -> str:
    return hashlib.sha256(_canonical(payload) + archive).hexdigest()


def _safe_root(root: Path) -> None:
    # Do not resolve away a symlink before checking its actual path components.
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise CheckpointUnavailable(
            "FORK_CHECKPOINT_UNSAFE_FILE", "工作区包含符号链接，不能建立检查点。"
        )


def _archive(root: Path) -> tuple[bytes, int, int]:
    _safe_root(root)
    buffer = io.BytesIO()
    total = count = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            files.sort()
            for name in [*dirs, *files]:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise CheckpointUnavailable(
                        "FORK_CHECKPOINT_UNSAFE_FILE", "工作区包含非普通文件。"
                    )
                relative = path.relative_to(root).as_posix()
                count += 1
                if count > MAX_FILES:
                    raise CheckpointUnavailable(
                        "FORK_CHECKPOINT_LIMIT_EXCEEDED", "工作区文件数量超过检查点限制。"
                    )
                if stat.S_ISDIR(mode):
                    archive.writestr(zipfile.ZipInfo(relative + "/"), b"")
                    continue
                opened = open_harness_artifact(root, relative)
                try:
                    chunks = []
                    for chunk in opened.iter_bytes():
                        total += len(chunk)
                        if total > MAX_SNAPSHOT_BYTES:
                            raise CheckpointUnavailable(
                                "FORK_CHECKPOINT_LIMIT_EXCEEDED", "工作区超过检查点容量限制。"
                            )
                        chunks.append(chunk)
                    info = zipfile.ZipInfo(relative)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | stat.S_IMODE(mode)) << 16
                    archive.writestr(info, b"".join(chunks))
                finally:
                    opened.close()
    return buffer.getvalue(), total, count


def prune_checkpoints(db: Session, session: ChatSession) -> None:
    rows = sorted(
        _rows(db, HarnessSessionCheckpoint, session),
        key=lambda row: (row.created_at, row.id),
        reverse=True,
    )
    count = total = 0
    now = utc_now()
    for row in rows:
        if row.status != "ready":
            continue
        count += 1
        total += row.size_bytes
        if row.expires_at <= now or count > MAX_CHECKPOINTS or total > MAX_SESSION_BYTES:
            row.status = "expired"
            row.error_code = "FORK_CHECKPOINT_EXPIRED"
            row.payload_json = {}
            row.archive_bytes = None
            row.size_bytes = 0
            db.add(row)


def cleanup_expired_checkpoints(db: Session) -> int:
    """Stage expiry in the existing hourly Public API maintenance transaction."""
    rows = db.exec(
        select(HarnessSessionCheckpoint).where(
            HarnessSessionCheckpoint.status == "ready",
            HarnessSessionCheckpoint.expires_at <= utc_now(),
        )
    ).all()
    for row in rows:
        row.status = "expired"
        row.error_code = "FORK_CHECKPOINT_EXPIRED"
        row.payload_json = {}
        row.archive_bytes = None
        row.size_bytes = 0
        db.add(row)
    return len(rows)


def capture_checkpoint(db: Session, session: ChatSession) -> HarnessSessionCheckpoint | None:
    """Called under the runtime session lease, before input and after finalization.

    Failure to snapshot files disables this boundary's fork, never pretends that
    current files are historical files and never turns a successful chat into failure.
    All private state stays in StaffDeck's own SQLite transaction/storage.
    """
    if session.channel != "public_api":
        return None
    row = HarnessSessionCheckpoint(
        tenant_id=session.tenant_id,
        session_id=session.id,
        expires_at=utc_now() + timedelta(days=RETENTION_DAYS),
    )
    try:
        row.payload_json = _state(db, session)
        state_size = len(_canonical(row.payload_json))
        if state_size > MAX_STATE_BYTES:
            raise CheckpointUnavailable(
                "FORK_CHECKPOINT_LIMIT_EXCEEDED", "会话状态超过检查点容量限制。"
            )
        row.archive_bytes, file_size, _ = _archive(
            harness_session_workspace_path(
                tenant_id=session.tenant_id, session_id=session.id, db=db
            )
        )
        row.size_bytes = state_size + file_size
        if row.size_bytes > MAX_SNAPSHOT_BYTES:
            raise CheckpointUnavailable("FORK_CHECKPOINT_LIMIT_EXCEEDED", "检查点超过容量限制。")
        row.digest = _digest(row.payload_json, row.archive_bytes)
        latest = db.exec(
            select(HarnessSessionCheckpoint)
            .where(
                HarnessSessionCheckpoint.session_id == session.id,
                HarnessSessionCheckpoint.tenant_id == session.tenant_id,
                HarnessSessionCheckpoint.before_message_id.is_(None),
                HarnessSessionCheckpoint.status == "ready",
                HarnessSessionCheckpoint.expires_at > utc_now(),
            )
            .order_by(HarnessSessionCheckpoint.created_at.desc())
        ).first()
        if latest and latest.digest == row.digest:
            return latest
    except (CheckpointUnavailable, OSError, HarnessArtifactAccessError) as exc:
        row.status = "unavailable"
        row.error_code = getattr(exc, "code", "FORK_CHECKPOINT_IO_ERROR")
        row.payload_json = {}
        row.archive_bytes = None
        row.size_bytes = 0
    db.add(row)
    db.flush()
    prune_checkpoints(db, session)
    db.commit()
    return row


def bind_checkpoint(db: Session, checkpoint, message_id: str) -> None:
    if checkpoint is not None:
        checkpoint.before_message_id = message_id
        db.add(checkpoint)
        db.commit()


def checkpoint_for_message(db, session, message_id: str) -> HarnessSessionCheckpoint:
    prune_checkpoints(db, session)
    db.commit()
    row = db.exec(
        select(HarnessSessionCheckpoint).where(
            HarnessSessionCheckpoint.tenant_id == session.tenant_id,
            HarnessSessionCheckpoint.session_id == session.id,
            HarnessSessionCheckpoint.before_message_id == message_id,
        )
    ).first()
    if row is None:
        raise CheckpointUnavailable(
            "FORK_CHECKPOINT_MISSING", "这条消息之前没有检查点，无法从这里分叉。"
        )
    if row.status != "ready":
        raise CheckpointUnavailable(
            row.error_code or "FORK_CHECKPOINT_MISSING", "这条消息之前的检查点不可用。"
        )
    if row.archive_bytes is None or _digest(row.payload_json, row.archive_bytes) != row.digest:
        raise CheckpointUnavailable("FORK_CHECKPOINT_INVALID", "检查点校验失败。")
    return row


def _remap(value, ids):
    if isinstance(value, dict):
        return {ids.get(key, key): _remap(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap(item, ids) for item in value]
    if isinstance(value, str):
        return ids.get(value, value)
    return value


def _restore_archive(archive: bytes, root: Path, segments: dict[str, str]) -> int:
    _safe_root(root)
    root.mkdir(parents=True, exist_ok=True)
    count = total = 0
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for item in source.infolist():
            parts = PurePosixPath(item.filename).parts
            if not parts or item.filename.startswith("/") or any(p in {".", ".."} for p in parts):
                raise CheckpointUnavailable("FORK_CHECKPOINT_INVALID", "检查点文件路径无效。")
            total += item.file_size
            if total > MAX_SNAPSHOT_BYTES or len(source.infolist()) > MAX_FILES:
                raise CheckpointUnavailable("FORK_CHECKPOINT_INVALID", "检查点文件超过限制。")
            target = root.joinpath(segments.get(parts[0], parts[0]), *parts[1:])
            _safe_root(target)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    output.write(source.read(item))
                target.chmod((item.external_attr >> 16) & 0o777 or 0o600)
                count += 1
    return count


def restore_checkpoint(
    db, source: ChatSession, checkpoint, target_id: str, principal
) -> tuple[ChatSession, dict]:
    """Stage a new private branch. Caller commits once with its idempotency receipt."""
    payload = checkpoint.payload_json
    ids = {source.id: target_id}
    for group, prefix in (("messages", "msg"), ("frames", "htask"), ("loops", "hloop")):
        for item in payload[group]:
            ids[item["id"]] = new_id(prefix)
    for frame in payload["frames"]:
        ids.setdefault(frame["task_id"], new_id("task"))
    for loop in payload["loops"]:
        if loop.get("workspace_scope_id"):
            ids.setdefault(loop["workspace_scope_id"], new_id("forkws"))
    segments = {harness_path_segment(old): harness_path_segment(new) for old, new in ids.items()}
    for message in payload["messages"]:
        segments[harness_path_segment("turn-" + message["id"])] = harness_path_segment(
            "turn-" + ids[message["id"]]
        )
    state = _remap(payload["session"], ids)
    state.update(
        id=target_id,
        user_id=principal.actor_user.id,
        channel="public_api",
        external_conv_id=None,
        channel_binding_id=None,
        channel_account_key=None,
        team_id=None,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    state["channel_target_json"] = (
        {
            "_platform_subject": principal.platform_subject,
            "_platform_credential": principal.credential_id,
        }
        if principal.platform_subject
        else None
    )
    if state["status"] in {"running", "handoff"}:
        state["status"] = "active"
    target = ChatSession.model_validate(state)
    from staffdeck_harness.runtime.session_binding import bind_session

    bind_session(db, target, created=True)
    db.add(target)
    for message in payload["messages"]:
        cloned = dict(message)
        cloned.update(id=ids[message["id"]], session_id=target_id)
        cloned["metadata_json"] = _remap(message.get("metadata_json", {}), ids)
        db.add(Message.model_validate(cloned))
    external_waits = 0
    for frame in payload["frames"]:
        cloned = _remap(frame, ids)
        cloned.update(lease_owner=None, lease_expires_at=None, attempt_no=0, state_version=1)
        if cloned["status"] in {"handoff", "waiting_external_task"}:
            external_waits += 1
            cloned["status"] = "awaiting_user"
            cloned["error_json"] = {
                "code": "FORK_RECONFIRM_REQUIRED",
                "message": "原分支的外部动作不会撤回或重新执行，请在此分支重新确认。",
            }
        db.add(HarnessTaskFrameRecord.model_validate(cloned))
    for loop in payload["loops"]:
        cloned = _remap(loop, ids)
        cloned["loop_key"] = (
            f"general:{target_id}"
            if loop["kind"] != "sop"
            else f"sop:{cloned['owner_task_frame_record_id']}"
        )
        cloned.update(last_run_id=None, state_version=1)
        db.add(HarnessAgentLoopRecord.model_validate(cloned))
    db.flush()
    file_count = _restore_archive(
        checkpoint.archive_bytes,
        harness_session_workspace_path(tenant_id=source.tenant_id, session_id=target_id, db=db),
        segments,
    )
    # Keep immutable snapshots for inherited user messages too. Each copy owns
    # its bytes; deleting the source session cannot break the child branch.
    historical = _rows(db, HarnessSessionCheckpoint, source)
    copied_boundaries = 0
    for old in historical:
        if old.before_message_id not in ids or old.status != "ready":
            continue
        old_payload = _remap(old.payload_json, ids)
        # User/assistant message text remains byte-for-byte historical content.
        for old_message, new_message in zip(old.payload_json["messages"], old_payload["messages"]):
            new_message["content"] = old_message["content"]
        old_payload["session"]["id"] = target_id
        rewritten = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(old.archive_bytes)) as inp,
            zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as out,
        ):
            for entry in inp.infolist():
                parts = PurePosixPath(entry.filename).parts
                name = "/".join((segments.get(parts[0], parts[0]), *parts[1:]))
                if entry.is_dir():
                    name += "/"
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = entry.external_attr
                out.writestr(info, inp.read(entry))
        archive = rewritten.getvalue()
        db.add(
            HarnessSessionCheckpoint(
                tenant_id=source.tenant_id,
                session_id=target_id,
                before_message_id=ids[old.before_message_id],
                payload_json=old_payload,
                archive_bytes=archive,
                digest=_digest(old_payload, archive),
                size_bytes=old.size_bytes,
                created_at=old.created_at,
                expires_at=old.expires_at,
            )
        )
        copied_boundaries += 1
    return target, {
        "messages": len(payload["messages"]),
        "session_state": True,
        "task_frames": len(payload["frames"]),
        "agent_loops": len(payload["loops"]),
        "workspace_files": file_count,
        "historical_checkpoints": copied_boundaries,
        "external_effects": False,
        "long_term_memories": False,
        "external_waits_require_confirmation": external_waits,
    }
