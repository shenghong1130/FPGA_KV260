from __future__ import annotations

import base64

import pytest

from .conftest import password_headers, predict, upload

pytestmark = pytest.mark.asyncio


async def test_multipart_image_is_forwarded_as_base64_without_leaking(test_context) -> None:
    client, fake = test_context
    await upload(client, student="image-student")
    image = b"\x89PNG\r\n\x1a\nimage-bytes"
    response = await client.post(
        "/predict",
        data={"student_id": "image-student"},
        files={"image": ("flower.png", image, "image/png")},
        headers=password_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["result"]["flower_api"] == "bailianhua"
    assert "image_base64" not in str(body["result"])
    assert "board" not in body["result"]
    assert "lease_id" not in body["result"]
    assert "worker" not in body["result"]
    assert fake.predict_payloads[-1] == {
        "image_base64": base64.b64encode(image).decode("ascii"),
        "content_type": "image/png",
    }


async def test_multipart_image_can_queue(test_context) -> None:
    client, _ = test_context
    for student in ("a", "b", "c", "d"):
        await upload(client, student=student)
    for student in ("a", "b", "c"):
        assert (await predict(client, student)).status_code == 200
    response = await client.post(
        "/predict",
        data={"student_id": "d"},
        files={"image": ("flower.jpg", b"jpeg", "image/jpeg")},
        headers=password_headers(),
    )
    assert response.status_code == 202
    assert response.json()["status"] == "queued"


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"image": ("empty.png", b"", "image/png")}, 422),
        ({"image": ("bad.gif", b"gif", "image/gif")}, 422),
        ({}, 422),
    ],
)
async def test_multipart_image_validation(test_context, files, expected) -> None:
    client, _ = test_context
    await upload(client, student="student-a")
    response = await client.post(
        "/predict",
        data={"student_id": "student-a"},
        files=files,
        headers=password_headers(),
    )
    assert response.status_code == expected


async def test_multipart_image_size_limit(test_context) -> None:
    client, _ = test_context
    await upload(client, student="student-a")
    response = await client.post(
        "/predict",
        data={"student_id": "student-a"},
        files={"image": ("large.png", b"x" * 65, "image/png")},
        headers=password_headers(),
    )
    assert response.status_code == 413


async def test_existing_json_predict_contract_is_preserved(test_context) -> None:
    client, fake = test_context
    await upload(client, student="json-student")
    response = await client.post(
        "/predict",
        json={"student_id": "json-student", "payload": {"value": 7}},
        headers=password_headers(),
    )
    assert response.status_code == 200
    assert response.json()["result"]["input"] == {"value": 7}
    assert fake.predict_payloads[-1] == {"value": 7}
