from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select

from ..artifact_store import ArtifactStore, ArtifactValidationError
from ..db_models import Artifact
from ..schemas import ArtifactResponse

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/fpga/artifacts", tags=["artifacts"])


def _response(artifact: Artifact) -> ArtifactResponse:
    return ArtifactResponse(
        artifact_id=artifact.id,
        student_id=artifact.student_id,
        version=artifact.version,
        status=artifact.status.lower(),
        bit_sha256=artifact.bit_sha256,
        hwh_sha256=artifact.hwh_sha256,
        bit_size=artifact.bit_size,
        hwh_size=artifact.hwh_size,
        created_at=artifact.created_at,
    )


@router.post("", response_model=ArtifactResponse, status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    request: Request,
    student_id: str = Form(min_length=1, max_length=128),
    bit: UploadFile = File(),
    hwh: UploadFile = File(),
) -> ArtifactResponse:
    store: ArtifactStore = request.app.state.services.artifact_store
    try:
        artifact = await store.create(student_id, bit, hwh)
    except ArtifactValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await bit.close()
        await hwh.close()
    LOGGER.info(
        "artifact uploaded: artifact_id=%s student_id=%s",
        artifact.id,
        artifact.student_id,
    )
    return _response(artifact)


@router.get("", response_model=list[ArtifactResponse])
async def list_artifacts(request: Request) -> list[ArtifactResponse]:
    sessions = request.app.state.services.database.sessions
    with sessions() as database:
        artifacts = list(
            database.scalars(select(Artifact).order_by(Artifact.created_at)).all()
        )
    return [_response(artifact) for artifact in artifacts]


@router.get("/{artifact_id}", response_model=ArtifactResponse)
async def get_artifact(artifact_id: str, request: Request) -> ArtifactResponse:
    sessions = request.app.state.services.database.sessions
    with sessions() as database:
        artifact = database.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return _response(artifact)
