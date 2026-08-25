from __future__ import annotations

import pytest

from .conftest import upload

pytestmark = pytest.mark.asyncio


async def test_valid_upload_and_queries(test_context) -> None:
    client, _ = test_context
    artifact = await upload(client)
    assert artifact["status"] == "ready"
    detail = await client.get(f"/fpga/artifacts/{artifact['artifact_id']}")
    assert detail.status_code == 200
    listing = await client.get("/fpga/artifacts")
    assert [item["artifact_id"] for item in listing.json()] == [artifact["artifact_id"]]
    assert "bit_path" not in detail.json()


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
        data={"student_id": "student", "project_name": "demo", "version": "v1"},
        files={"bit": (bit_name, bit), "hwh": (hwh_name, hwh)},
    )
    assert response.status_code == 422
