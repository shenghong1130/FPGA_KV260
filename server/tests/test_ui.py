from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_dashboard_index(test_context) -> None:
    client, _ = test_context
    response = await client.get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "KV260 FPGA 共享计算平台" in response.text


async def test_dashboard_static_assets(test_context) -> None:
    client, _ = test_context
    css = await client.get("/ui/dashboard.css")
    javascript = await client.get("/ui/dashboard.js")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert javascript.status_code == 200
    assert "javascript" in javascript.headers["content-type"]


async def test_ui_path_redirects_to_trailing_slash(test_context) -> None:
    client, _ = test_context
    response = await client.get("/ui", follow_redirects=False)
    assert response.status_code in {200, 307, 308}
    if response.is_redirect:
        assert response.headers["location"].endswith("/ui/")


async def test_docs_and_health_remain_available(test_context) -> None:
    client, _ = test_context
    docs = await client.get("/docs")
    health = await client.get("/health")
    assert docs.status_code == 200
    assert "text/html" in docs.headers["content-type"]
    assert health.status_code == 200
    assert health.json()["ok"] is True
