from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..admin_auth import require_admin_token
from ..artifact_cleanup import ArtifactCleanupService
from ..schemas import ArtifactCleanupPreview, ArtifactCleanupResult

router = APIRouter(prefix="/admin/artifacts", tags=["admin-artifacts"])


@router.get(
    "/cleanup-preview",
    response_model=ArtifactCleanupPreview,
    dependencies=[Depends(require_admin_token)],
)
async def cleanup_preview(request: Request) -> ArtifactCleanupPreview:
    service: ArtifactCleanupService = request.app.state.services.artifact_cleanup
    return ArtifactCleanupPreview(**service.preview())


@router.post(
    "/cleanup",
    response_model=ArtifactCleanupResult,
    dependencies=[Depends(require_admin_token)],
)
async def cleanup(request: Request) -> ArtifactCleanupResult:
    service: ArtifactCleanupService = request.app.state.services.artifact_cleanup
    return ArtifactCleanupResult(**await service.execute())
