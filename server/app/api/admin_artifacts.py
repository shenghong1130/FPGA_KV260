from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..admin_auth import require_admin_token
from ..artifact_cleanup import (
    ArtifactCleanupService,
    ArtifactDeleteConflictError,
    ArtifactDeleteFileError,
    ArtifactDeleteNotFoundError,
)
from ..schemas import ArtifactCleanupPreview, ArtifactCleanupResult, ArtifactDeleteResult

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


@router.delete(
    "/{artifact_id}",
    response_model=ArtifactDeleteResult,
    dependencies=[Depends(require_admin_token)],
)
async def delete_artifact(
    artifact_id: str, request: Request
) -> ArtifactDeleteResult:
    service: ArtifactCleanupService = request.app.state.services.artifact_cleanup
    try:
        result = await service.delete_artifact(artifact_id)
    except ArtifactDeleteNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="artifact not found",
        ) from exc
    except ArtifactDeleteConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ArtifactDeleteFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"artifact file deletion failed: {exc}",
        ) from exc
    return ArtifactDeleteResult(**result)
