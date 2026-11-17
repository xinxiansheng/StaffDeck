from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.core.harness_session_cleanup import (
    harness_session_workspace_path,
    harness_task_workspace_path,
)
from app.core.harness_session_lease import HarnessSessionLeaseStore
from app.core.session_checkpoints import bind_checkpoint, capture_checkpoint
from app.db.models import (
    APIJob,
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessSessionCheckpoint,
    HarnessTaskFrameRecord,
    MemoryRecord,
    Message,
    utc_now,
)
from tests.test_platform_subject_isolation import create_session, headers
from tests.test_platform_subject_isolation import platform as platform  # noqa: PLC0414


def seed_boundary(platform, **session_body):
    client, engine, token = platform
    sid = create_session(client, token, **session_body)
    with Session(engine) as db:
        session = db.get(ChatSession, sid)
        initial = capture_checkpoint(db, session)
        first = Message(
            tenant_id=session.tenant_id,
            session_id=sid,
            role="user",
            content="original first request",
        )
        db.add(first)
        db.commit()
        bind_checkpoint(db, initial, first.id)
        answer = Message(
            tenant_id=session.tenant_id, session_id=sid, role="assistant", content="first reply"
        )
        frame = HarnessTaskFrameRecord(
            tenant_id=session.tenant_id,
            session_id=sid,
            source_turn_id=first.id,
            task_id="original-task",
            status="awaiting_user",
            kind="sop",
            skill_id="review",
            step_id="ask",
            slots_json={"budget": 10},
            task_requirement_json={"known_slots": {"budget": 10}},
        )
        db.add(frame)
        db.flush()
        loop = HarnessAgentLoopRecord(
            tenant_id=session.tenant_id,
            session_id=sid,
            loop_key="sop:" + frame.id,
            owner_task_frame_record_id=frame.id,
            workspace_scope_id=frame.id,
            kind="sop",
            status="suspended",
            checkpoint_json={
                "engine": "harness_v3",
                "step_id": "ask",
                "transcript": [{"role": "assistant", "content": "first reply"}],
            },
        )
        db.add(loop)
        db.flush()
        frame.agent_loop_id = loop.id
        session.slots_json = {"budget": 10}
        session.skill_stack_json = [{"skill_id": "review", "step_id": "ask"}]
        session.active_skill_id = "review"
        session.active_step_id = "ask"
        session.context_state_json = {**session.context_state_json, "task_id": frame.task_id}
        db.add_all(
            [
                answer,
                frame,
                session,
                MemoryRecord(
                    tenant_id=session.tenant_id,
                    user_id=session.user_id,
                    agent_id=session.agent_id,
                    content="shared long-term fact",
                ),
            ]
        )
        db.commit()
        workspace = harness_task_workspace_path(
            tenant_id=session.tenant_id, session_id=sid, task_frame_id=frame.id, db=db
        )
        workspace.mkdir(parents=True)
        (workspace / "report.txt").write_text("historical bytes")
        (workspace / "report.txt").chmod(0o640)
        (workspace / ".cache").mkdir()
        (workspace / ".cache/state").write_text("historical hidden bytes")
        (workspace / "empty").mkdir()
        checkpoint = capture_checkpoint(db, session)
        pivot = Message(
            tenant_id=session.tenant_id, session_id=sid, role="user", content="request to edit"
        )
        db.add(pivot)
        db.commit()
        bind_checkpoint(db, checkpoint, pivot.id)
        db.add(
            Message(
                tenant_id=session.tenant_id,
                session_id=sid,
                role="assistant",
                content="future result",
            )
        )
        session.slots_json = {"budget": 99}
        frame.slots_json = {"budget": 99}
        db.add_all([session, frame])
        db.commit()
        (workspace / "report.txt").write_text("future bytes")
        (workspace / "future.txt").write_text("must not be copied")
        return sid, pivot.id, first.id, workspace, checkpoint.id


def fork(platform, sid, pivot, key="fork-1", user="alice"):
    client, _, token = platform
    return client.post(
        f"/agents/agent_test/sessions/{sid}:fork",
        headers=headers(token, user, **{"Idempotency-Key": key}),
        json={"beforeMessageId": pivot},
    )


def test_fork_restores_historical_state_files_and_inherited_boundaries(platform):
    sid, pivot, first, original_workspace, _ = seed_boundary(platform)
    result = fork(platform, sid, pivot)
    assert result.status_code == 201, result.text
    data = result.json()
    assert data["copied"]["messages"] == 2 and data["copied"]["workspace_files"] == 2
    assert data["copied"]["external_effects"] is False
    assert data["externalEffectsReverted"] is False and data["requiresNewConfirmation"] is True
    _, engine, _ = platform
    with Session(engine) as db:
        target = db.get(ChatSession, data["sessionId"])
        assert target.slots_json == {"budget": 10}
        assert target.skill_stack_json == [{"skill_id": "review", "step_id": "ask"}]
        assert target.active_step_id == "ask"
        messages = db.exec(
            select(Message).where(Message.session_id == target.id).order_by(Message.created_at)
        ).all()
        assert [m.content for m in messages] == ["original first request", "first reply"]
        assert all(m.id not in {pivot, first} for m in messages)
        frame = db.exec(
            select(HarnessTaskFrameRecord).where(HarnessTaskFrameRecord.session_id == target.id)
        ).one()
        assert frame.slots_json == {"budget": 10} and frame.source_turn_id == messages[0].id
        assert frame.lease_owner is None and frame.attempt_no == 0
        loop = db.get(HarnessAgentLoopRecord, frame.agent_loop_id)
        assert loop.owner_task_frame_record_id == frame.id and loop.workspace_scope_id == frame.id
        restored = harness_task_workspace_path(
            tenant_id=target.tenant_id,
            session_id=target.id,
            task_frame_id=loop.workspace_scope_id,
            db=db,
        )
        assert (restored / "report.txt").read_text() == "historical bytes"
        assert (restored / "report.txt").stat().st_mode & 0o777 == 0o640
        assert (restored / ".cache/state").read_text() == "historical hidden bytes"
        assert (restored / "empty").is_dir() and not (restored / "future.txt").exists()
        assert original_workspace.joinpath("report.txt").read_text() == "future bytes"
        assert db.get(ChatSession, sid).slots_json == {"budget": 99}
        assert len(db.exec(select(MemoryRecord)).all()) == 1
        assert not db.exec(select(APIJob).where(APIJob.session_id == target.id)).all()
        inherited_id = messages[0].id
    # A fork can itself fork an inherited user message, even after parent changes.
    second = fork(platform, data["sessionId"], inherited_id, "nested")
    assert second.status_code == 201, second.text
    assert second.json()["copied"]["messages"] == 0


def test_fork_receipt_is_atomic_and_scoped_to_user(platform):
    sid, pivot, first, _, _ = seed_boundary(platform)
    one = fork(platform, sid, pivot)
    two = fork(platform, sid, pivot)
    assert one.status_code == two.status_code == 201 and one.json() == two.json()
    assert fork(platform, sid, first).status_code == 409
    assert fork(platform, sid, pivot, user="bob").status_code == 404
    _, engine, _ = platform
    with Session(engine) as db:
        assert len(db.exec(select(ChatSession)).all()) == 2


def test_fork_requires_user_message_checkpoint_and_key(platform):
    sid, pivot, _, _, _ = seed_boundary(platform)
    client, engine, token = platform
    with Session(engine) as db:
        assistant = db.exec(
            select(Message).where(Message.session_id == sid, Message.role == "assistant")
        ).first()
        aid = assistant.id
        legacy = Message(
            tenant_id="sd_tenant", session_id=sid, role="user", content="no checkpoint"
        )
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    assert fork(platform, sid, aid).status_code == 422
    missing = fork(platform, sid, legacy_id)
    assert missing.status_code == 409 and "FORK_CHECKPOINT_MISSING" in missing.text
    assert fork(platform, sid, "missing").status_code == 404
    no_key = client.post(
        f"/agents/agent_test/sessions/{sid}:fork",
        headers=headers(token),
        json={"beforeMessageId": pivot},
    )
    assert no_key.status_code == 400 and "IDEMPOTENCY_KEY_REQUIRED" in no_key.text


def test_fork_rejects_queued_job_and_cross_process_session_lease(platform):
    sid, pivot, _, _, _ = seed_boundary(platform)
    client, engine, token = platform
    with Session(engine) as db:
        lease_store = HarnessSessionLeaseStore(db)
        lease = lease_store.acquire(db.get(ChatSession, sid))
        assert fork(platform, sid, pivot).status_code == 409
        lease_store.release(lease)
    run = client.post(
        "/agents/agent_test/runs",
        headers=headers(token),
        json={"session_id": sid, "input": "run later"},
    )
    assert run.status_code == 202, run.text
    response = fork(platform, sid, pivot)
    assert response.status_code == 409 and "SESSION_BUSY" in response.text


def test_partial_file_restore_rolls_back_and_same_key_can_retry(platform, monkeypatch):
    sid, pivot, _, _, _ = seed_boundary(platform)
    from app.core import session_checkpoints

    restore = session_checkpoints._restore_archive

    def interrupted(*args, **kwargs):
        restore(*args, **kwargs)
        raise OSError("synthetic disk error")

    monkeypatch.setattr(session_checkpoints, "_restore_archive", interrupted)
    response = fork(platform, sid, pivot)
    assert response.status_code == 503 and "FORK_STORAGE_FAILED" in response.text
    _, engine, _ = platform
    with Session(engine) as db:
        assert len(db.exec(select(ChatSession)).all()) == 1
        root = harness_session_workspace_path(tenant_id="sd_tenant", session_id=sid, db=db).parent
        assert len(list(root.iterdir())) == 1
    monkeypatch.setattr(session_checkpoints, "_restore_archive", restore)
    assert fork(platform, sid, pivot).status_code == 201


@pytest.mark.parametrize("kind", ["expired", "tampered"])
def test_unavailable_snapshot_cannot_fall_back_to_current_state(platform, kind):
    sid, pivot, _, _, checkpoint_id = seed_boundary(platform)
    _, engine, _ = platform
    with Session(engine) as db:
        checkpoint = db.get(HarnessSessionCheckpoint, checkpoint_id)
        if kind == "expired":
            checkpoint.expires_at = utc_now() - timedelta(seconds=1)
        else:
            checkpoint.archive_bytes = b"invalid archive"
        db.add(checkpoint)
        db.commit()
    result = fork(platform, sid, pivot)
    code = "FORK_CHECKPOINT_EXPIRED" if kind == "expired" else "FORK_CHECKPOINT_INVALID"
    assert result.status_code == 409 and code in result.text


@pytest.mark.parametrize("kind", ["size", "symlink"])
def test_snapshot_limit_or_symlink_marks_boundary_unavailable(
    platform, monkeypatch, tmp_path, kind
):
    sid, _, _, workspace, _ = seed_boundary(platform)
    from app.core import session_checkpoints

    if kind == "size":
        monkeypatch.setattr(session_checkpoints, "MAX_SNAPSHOT_BYTES", 1)
    else:
        secret = tmp_path / "outside.txt"
        secret.write_text("outside")
        (workspace / "escape.txt").symlink_to(secret)
    _, engine, _ = platform
    with Session(engine) as db:
        checkpoint = capture_checkpoint(db, db.get(ChatSession, sid))
        assert checkpoint.status == "unavailable" and checkpoint.archive_bytes is None
        assert checkpoint.payload_json == {}


def test_session_deletion_removes_its_checkpoint_bytes(platform):
    sid, _, _, _, _ = seed_boundary(platform)
    from app.session.cleanup import purge_chat_session_records

    _, engine, _ = platform
    with Session(engine) as db:
        purge_chat_session_records(db, db.get(ChatSession, sid))
        db.commit()
        assert not db.exec(
            select(HarnessSessionCheckpoint).where(HarnessSessionCheckpoint.session_id == sid)
        ).all()


def test_expiry_maintenance_keeps_fork_receipt_and_releases_snapshot_bytes(platform, monkeypatch):
    sid, pivot, _, _, checkpoint_id = seed_boundary(platform)
    result = fork(platform, sid, pivot)
    assert result.status_code == 201
    _, engine, _ = platform
    from app.public_api import jobs

    monkeypatch.setattr(jobs, "engine", engine)
    with Session(engine) as db:
        checkpoint = db.get(HarnessSessionCheckpoint, checkpoint_id)
        checkpoint.expires_at = utc_now() - timedelta(seconds=1)
        db.add(checkpoint)
        db.commit()
    jobs.cleanup_public_api_records()
    with Session(engine) as db:
        checkpoint = db.get(HarnessSessionCheckpoint, checkpoint_id)
        assert checkpoint.status == "expired" and checkpoint.archive_bytes is None
        assert checkpoint.payload_json == {} and checkpoint.size_bytes == 0
    assert fork(platform, sid, pivot).json() == result.json()


@pytest.mark.parametrize("quota", ["count", "bytes"])
def test_session_quota_expires_old_snapshots_instead_of_using_current_files(
    platform, monkeypatch, quota
):
    sid, pivot, _, _, old_id = seed_boundary(platform)
    from app.core import session_checkpoints

    _, engine, _ = platform
    with Session(engine) as db:
        source = db.get(ChatSession, sid)
        latest = capture_checkpoint(db, source)
        if quota == "count":
            monkeypatch.setattr(session_checkpoints, "MAX_CHECKPOINTS", 1)
        else:
            monkeypatch.setattr(session_checkpoints, "MAX_SESSION_BYTES", latest.size_bytes + 1)
        session_checkpoints.prune_checkpoints(db, source)
        db.commit()
        assert db.get(HarnessSessionCheckpoint, old_id).archive_bytes is None
        assert db.get(HarnessSessionCheckpoint, latest.id).status == "ready"
    response = fork(platform, sid, pivot)
    assert response.status_code == 409 and "FORK_CHECKPOINT_EXPIRED" in response.text


def test_existing_sqlite_database_gains_checkpoint_table_without_rewriting_history(
    tmp_path, monkeypatch
):
    from sqlalchemy import inspect
    from sqlmodel import SQLModel, create_engine

    from app.db import database

    path = tmp_path / "legacy-staffdeck.db"
    engine = create_engine("sqlite:///" + str(path))
    tables = [
        table
        for table in SQLModel.metadata.sorted_tables
        if table.name != "harness_session_checkpoints"
    ]
    SQLModel.metadata.create_all(engine, tables=tables)
    with Session(engine) as db:
        db.add(ChatSession(id="legacy", tenant_id="tenant", user_id="owner", channel="public_api"))
        db.add(
            Message(
                id="legacy-message",
                tenant_id="tenant",
                session_id="legacy",
                role="user",
                content="keep history",
            )
        )
        db.commit()
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "database_url", "sqlite:///" + str(path))
    database.init_db()
    database.init_db()
    assert "harness_session_checkpoints" in inspect(engine).get_table_names()
    with Session(engine) as db:
        assert db.get(Message, "legacy-message").content == "keep history"
        assert not db.exec(select(HarnessSessionCheckpoint)).all()
    engine.dispose()


@pytest.mark.parametrize("expired", [False, True])
def test_orphan_turn_fences_only_while_its_lease_is_live(platform, expired):
    from app.db.models import HarnessTurnRecord

    sid, pivot, _, _, _ = seed_boundary(platform)
    _, engine, _ = platform
    with Session(engine) as db:
        db.add(
            HarnessTurnRecord(
                tenant_id="sd_tenant",
                session_id=sid,
                client_turn_id="orphan",
                request_digest="synthetic",
                lease_owner="old-worker",
                lease_expires_at=utc_now() + timedelta(minutes=-1 if expired else 1),
            )
        )
        db.commit()
    response = fork(platform, sid, pivot)
    assert response.status_code == (201 if expired else 409), response.text


def test_queued_external_session_binding_also_blocks_fork(platform):
    sid, pivot, _, _, _ = seed_boundary(platform, external_session_id="known-thread")
    client, _, token = platform
    run = client.post(
        "/agents/agent_test/runs",
        headers=headers(token),
        json={"external_session_id": "known-thread", "input": "queued through external identity"},
    )
    assert run.status_code == 202, run.text
    response = fork(platform, sid, pivot)
    assert response.status_code == 409 and "SESSION_BUSY" in response.text
