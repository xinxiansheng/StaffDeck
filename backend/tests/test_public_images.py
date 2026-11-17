import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from app import paths
from app.db.models import APIJob
from app.public_api.images import MAX_IMAGE_BYTES, cleanup_expired_public_images
from tests.test_platform_subject_isolation import headers
from tests.test_platform_subject_isolation import platform as platform  # noqa: PLC0414 -- pytest fixture re-export

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _upload(client, token, *, data=PNG, filename="budget.png", content_type="image/png"):
    return client.post(
        "/agents/agent_test/images",
        headers=headers(token),
        files=[("files", (filename, data, content_type))],
    )


def _run(client, token, attachments, **kwargs):
    return client.post(
        "/agents/agent_test/runs",
        headers=headers(token, **kwargs),
        json={"input": "请读图片", "attachments": attachments},
    )


def _record():
    return next(paths.user_data_dir().rglob("public-image.json"))


def test_failed_upload_batch_discards_only_its_unreturned_images(platform):
    client, _, token = platform
    existing = _upload(client, token).json()[0]
    response = client.post("/agents/agent_test/images", headers=headers(token), files=[
        ("files", ("good.png", PNG, "image/png")),
        ("files", ("bad.png", b"not an image", "image/png")),
    ])
    assert response.status_code == 400
    records = list(paths.user_data_dir().rglob("public-image.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["attachment"]["id"] == existing["id"]


def test_image_reference_owner_bound_and_job_contains_no_raw_image(platform):
    client, engine, token = platform
    response = _upload(client, token)
    assert response.status_code == 201, response.text
    image = response.json()[0]
    assert image["id"].startswith("pimg_") and image["size"] == len(PNG)
    assert "data_url" not in image and "sandbox_path" not in image
    valid = _run(client, token, [{"id": image["id"]}])
    assert valid.status_code == 202, valid.text
    with Session(engine) as db:
        stored = db.get(APIJob, valid.json()["id"]).request_json
        assert stored["attachments"] == [{"id": image["id"]}]
        assert base64.b64encode(PNG).decode() not in json.dumps(stored)
    stolen = _run(client, token, [{"id": image["id"]}], user="bob")
    assert stolen.status_code == 404 and stolen.json()["code"] == "IMAGE_ATTACHMENT_NOT_FOUND"
    tenant = _run(client, token, [{"id": image["id"]}], tenant="foreign")
    assert tenant.status_code == 403
    raw = client.post(
        "/agents/agent_test/images",
        headers={"Authorization": "Bearer " + token},
        files=[("files", ("x.png", PNG, "image/png"))],
    )
    assert raw.status_code == 403


@pytest.mark.parametrize(
    "filename,content_type,data,status,code",
    [
        ("x.svg", "image/svg+xml", b"<svg></svg>", 415, "IMAGE_TYPE_UNSUPPORTED"),
        ("x.png", "image/png", b"not-image", 400, "IMAGE_ATTACHMENT_INVALID"),
        ("x.png", "image/jpeg", PNG, 400, "IMAGE_ATTACHMENT_INVALID"),
        ("x.png", "image/png", b"x" * (MAX_IMAGE_BYTES + 1), 413, "IMAGE_TOO_LARGE"),
        ("x.webp", "image/webp", b"RIFF" + b"\x00" * 4 + b"WEBP" + b"test", 201, None),
        ("x.jpg", "image/jpeg", b"\xff\xd8\xfftest\xff\xd9", 201, None),
    ],
)
def test_upload_limits_and_formats(platform, filename, content_type, data, status, code):
    client, _, token = platform
    response = _upload(client, token, data=data, filename=filename, content_type=content_type)
    assert response.status_code == status, response.text
    if code:
        assert response.json()["code"] == code


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "http://127.0.0.1/private"},
        {"data_url": "data:image/png;base64,AAAA"},
        {"sandbox_path": "/etc/passwd"},
    ],
)
def test_reference_cannot_override_content_or_path(platform, extra):
    client, _, token = platform
    image = _upload(client, token).json()[0]
    response = _run(client, token, [{"id": image["id"], **extra}])
    assert response.status_code == 400 and response.json()["code"] == "IMAGE_REFERENCE_INVALID"


def test_no_remote_download_or_unuploaded_inline_images(platform):
    client, _, token = platform
    assert (
        _run(client, token, [{"kind": "image", "url": "http://127.0.0.1/private"}]).status_code
        == 400
    )
    from app.session.attachments import parse_chat_attachment

    raw = parse_chat_attachment("x.png", "image/png", PNG).model_dump(mode="json")
    result = _run(client, token, [raw])
    assert result.status_code == 404 and result.json()["code"] == "IMAGE_ATTACHMENT_NOT_FOUND"


def test_expiry_and_corruption_fail_before_new_run_and_cleanup(platform):
    client, _, token = platform
    image = _upload(client, token).json()[0]
    record = _record()
    metadata = json.loads(record.read_text())
    metadata["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    record.write_text(json.dumps(metadata))
    expired = _run(client, token, [{"id": image["id"]}])
    assert expired.status_code == 410 and expired.json()["code"] == "IMAGE_ATTACHMENT_EXPIRED"
    assert not record.parent.exists()
    image = _upload(client, token).json()[0]
    record = _record()
    (record.parent / "payload").write_bytes(b"corrupt")
    corrupted = _run(client, token, [{"id": image["id"]}])
    assert corrupted.status_code == 409 and corrupted.json()["code"] == "IMAGE_ATTACHMENT_INVALID"
    assert cleanup_expired_public_images(now=datetime.now(UTC) + timedelta(days=8)) == 1
    assert not record.parent.exists()


def test_run_replay_does_not_require_expired_upload_again(platform):
    client, _, token = platform
    image = _upload(client, token).json()[0]
    ref = [{"id": image["id"]}]
    first = _run(client, token, ref, **{"Idempotency-Key": "once"})
    metadata = json.loads(_record().read_text())
    metadata["expires_at"] = "2000-01-01T00:00:00+00:00"
    _record().write_text(json.dumps(metadata))
    replay = _run(client, token, ref, **{"Idempotency-Key": "once"})
    assert (
        first.status_code == replay.status_code == 202 and first.json()["id"] == replay.json()["id"]
    )


def test_maximum_count_and_read_scope_enforced(platform):
    client, _, token = platform
    assert (
        client.post(
            "/agents/agent_test/images",
            headers=headers(token),
            files=[("files", ("x.png", PNG, "image/png"))] * 9,
        ).status_code
        == 400
    )
    assert _run(client, token, [{"id": "pimg_missing"}] * 9).status_code == 422


def test_image_reference_cannot_cross_employee(platform):
    from app.db.models import AgentProfile

    client, engine, token = platform
    image = _upload(client, token).json()[0]
    with Session(engine) as db:
        db.add(
            AgentProfile(
                id="agent_second",
                tenant_id="sd_tenant",
                name="Other employee",
                status="active",
                metadata_json={"owner_user_id": "service_owner"},
            )
        )
        db.commit()
    scoped = headers(token)
    subject = {
        "tenant_id": "platform_tenant",
        "user_id": "alice",
        "employee_id": "employee_second",
        "agent_id": "agent_second",
    }
    scoped["X-Platform-Subject"] = (
        base64.urlsafe_b64encode(json.dumps(subject).encode()).decode().rstrip("=")
    )
    response = client.post(
        "/agents/agent_second/runs",
        headers=scoped,
        json={"input": "请看图片", "attachments": [{"id": image["id"]}]},
    )
    assert response.status_code == 404 and response.json()["code"] == "IMAGE_ATTACHMENT_NOT_FOUND"


def test_existing_owner_scoped_attachment_upload_remains_usable(platform):
    client, _, token = platform
    upload = client.post(
        "/agents/agent_test/attachments",
        headers=headers(token),
        files=[("files", ("legacy.png", PNG, "image/png"))],
    )
    assert upload.status_code == 200, upload.text
    attachment = upload.json()[0]
    assert _run(client, token, [attachment]).status_code == 202
    assert _run(client, token, [attachment], user="bob").status_code == 404
    attachment["data_url"] = "data:image/png;base64," + base64.b64encode(PNG + b"forged").decode()
    assert _run(client, token, [attachment]).status_code == 400


def test_corrupt_staging_metadata_is_a_clear_attachment_failure(platform):
    client, _, token = platform
    image = _upload(client, token).json()[0]
    metadata_path = _record().parent / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["size"] = "corrupt-size"
    metadata_path.write_text(json.dumps(metadata))
    result = _run(client, token, [{"id": image["id"]}])
    assert result.status_code == 409
    assert result.json()["code"] == "IMAGE_ATTACHMENT_INVALID"
