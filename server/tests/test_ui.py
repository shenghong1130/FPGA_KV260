from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def test_dashboard_index(test_context) -> None:
    client, _ = test_context
    response = await client.get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "KV260 FPGA 共享计算平台" in response.text
    assert "最近计算请求" in response.text
    assert 'id="request-table"' in response.text
    assert 'id="student-request-query-form"' in response.text
    assert 'id="student-request-table"' in response.text
    assert "Admin Action Token" in response.text
    assert 'id="admin-token-input"' in response.text
    assert 'data-nav="events"' in response.text
    assert 'id="event-table"' in response.text
    assert 'id="cleanup-preview-button"' in response.text
    assert "旧版本清理" in response.text
    assert response.text.index('id="request-query-form"') < response.text.index(
        'id="student-request-query-form"'
    ) < response.text.index("Recent Predict Requests")
    assert "Global request listing is not available" not in response.text


async def test_dashboard_static_assets(test_context) -> None:
    client, _ = test_context
    css = await client.get("/ui/dashboard.css")
    javascript = await client.get("/ui/dashboard.js")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert javascript.status_code == 200
    assert "javascript" in javascript.headers["content-type"]
    assert 'apiFetch("/requests?limit=100")' in javascript.text
    assert "student_id=${encodeURIComponent(studentId)}" in javascript.text
    assert 'renderRequests($("#student-request-table")' in javascript.text
    assert 'state.view === "requests"' in javascript.text
    assert "Completed Requests" in javascript.text
    assert "Release Worker" in javascript.text
    assert "sessionStorage" in javascript.text
    assert '"X-Admin-Token": token' in javascript.text
    assert 'state.view === "events"' in javascript.text
    assert 'apiFetch("/admin/artifacts/cleanup-preview"' in javascript.text
    assert 'apiFetch("/admin/artifacts/cleanup"' in javascript.text


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
