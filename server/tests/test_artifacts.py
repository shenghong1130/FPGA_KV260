from __future__ import annotations

import asyncio

import pytest

from .conftest import upload

pytestmark = pytest.mark.asyncio


async def test_valid_upload_and_queries(test_context) -> None:
    client, _ = test_context
    artifact = await upload(client)
    assert artifact["status"] == "ready"
    assert artifact["version"] == "v1"
    assert "project_name" not in artifact
    detail = await client.get(f"/fpga/artifacts/{artifact['artifact_id']}")
    assert detail.status_code == 200
    assert detail.json()["version"] == "v1"
    assert "project_name" not in detail.json()
    listing = await client.get("/fpga/artifacts")
    assert [item["artifact_id"] for item in listing.json()] == [artifact["artifact_id"]]
    assert "bit_path" not in detail.json()


async def test_versions_increment_per_student(test_context) -> None:
    client, _ = test_context
    student_a = [await upload(client, student="student-a") for _ in range(4)]
    student_b = [await upload(client, student="student-b") for _ in range(2)]

    assert [artifact["version"] for artifact in student_a] == [
        "v1",
        "v2",
        "v3",
        "v4",
    ]
    assert [artifact["version"] for artifact in student_b] == ["v1", "v2"]
    assert all("project_name" not in artifact for artifact in student_a + student_b)


async def test_concurrent_upload_versions_are_unique(test_context) -> None:
    client, _ = test_context
    for _ in range(3):
        await upload(client, student="student-a")

    concurrent = await asyncio.gather(
        upload(client, student="student-a"),
        upload(client, student="student-a"),
    )

    assert {artifact["version"] for artifact in concurrent} == {"v4", "v5"}


@pytest.mark.parametrize(
    ("bit", "hwh", "bit_name", "hwh_name"),
    [
        (b"", b"<SYSTEM/>", "design.bit", "design.hwh"),
        (b"x", b"", "design.bit", "design.hwh"),
        (b"x", b"<SYSTEM/>", "design.bin", "design.hwh"),
        (b"x", b"<SYSTEM/>", "design.bit", "design.xml"),
        (b"x", b"not-xml", "design.bit", "design.hwh"),
    ],
)
async def test_invalid_uploads(
    test_context, bit: bytes, hwh: bytes, bit_name: str, hwh_name: str
) -> None:
    client, _ = test_context
    response = await client.post(
        "/fpga/artifacts",
        data={"student_id": "student"},
        files={"bit": (bit_name, bit), "hwh": (hwh_name, hwh)},
    )
    assert response.status_code == 422
