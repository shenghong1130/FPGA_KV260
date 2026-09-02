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
    assert 'id="worker-request-query-form"' in response.text
    assert 'id="worker-request-id-select"' in response.text
    assert 'id="worker-request-range"' in response.text
    assert 'id="worker-request-table"' in response.text
    assert 'id="student-request-query-form"' in response.text
    assert 'id="student-request-range"' in response.text
    assert 'id="student-request-table"' in response.text
    assert 'id="request-status-filter"' in response.text
    assert 'id="student-search-input"' in response.text
    assert 'id="student-table"' in response.text
    assert "Admin Action Token" in response.text
    assert 'id="admin-token-input"' in response.text
    assert 'data-nav="events"' in response.text
    assert 'id="event-table"' in response.text
    assert '<select id="event-type-filter">' in response.text
    assert '<input id="event-type-filter"' not in response.text
    assert 'id="cleanup-preview-button"' in response.text
    assert "旧版本清理" in response.text
    assert "操作 / Actions" in response.text
    assert response.text.index('id="worker-request-query-form"') < response.text.index(
        'id="student-request-query-form"'
    ) < response.text.index("Recent Predict Requests")
    assert "按 Request ID 查询" not in response.text
    assert 'id="request-query-form"' not in response.text
    assert 'id="request-password-input"' not in response.text
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
    assert "worker_id=${encodeURIComponent(board)}" in javascript.text
    assert "all=true" in javascript.text
    assert "limit=5" in javascript.text
    assert 'renderRequests($("#student-request-table")' in javascript.text
    assert 'renderRequests($("#worker-request-table")' in javascript.text
    assert 'apiFetch("/students")' in javascript.text
    assert "renderStudents" in javascript.text
    assert "requestStatusFilter" in javascript.text
    assert "navigateToStudentRequests" in javascript.text
    assert "navigateToStudentArtifacts" in javascript.text
    assert "navigateToStudentEvents" in javascript.text
    assert "navigateToWorker" in javascript.text
    assert "workerLink" in javascript.text
    assert "event.stopPropagation()" in javascript.text
    assert 'state.view === "requests"' in javascript.text
    assert "Completed Requests" in javascript.text
    assert "Release Worker" in javascript.text
    assert "sessionStorage" in javascript.text
    assert '"X-Admin-Token": token' in javascript.text
    assert 'state.view === "events"' in javascript.text
    assert 'apiFetch("/events/types")' in javascript.text
    assert '$("#request-query-form")' not in javascript.text
    assert 'apiFetch("/admin/artifacts/cleanup-preview"' in javascript.text
    assert 'apiFetch("/admin/artifacts/cleanup"' in javascript.text
    assert "DELETE /admin/artifacts/" in javascript.text
    assert "`/admin/artifacts/${encodeURIComponent(item.artifact_id)}`" in javascript.text
    assert 'method: "DELETE"' in javascript.text
    assert 'headers: { "X-Admin-Token": token }' in javascript.text
    assert "deleteArtifact" in javascript.text
    assert "X-Student-Password" not in javascript.text[
        javascript.text.index("async function deleteArtifact"):
        javascript.text.index("function renderRecentArtifacts")
    ]


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
