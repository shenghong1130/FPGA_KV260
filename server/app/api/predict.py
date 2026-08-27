from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from ..db_models import PredictRequestRecord, RequestStatus
from ..lease_manager import ArtifactNotFoundError, LeaseManager, RequestNotFoundError
from ..schemas import PredictResponse, PublicPredictRequest

router = APIRouter(tags=["predict"])


def response_for(record: PredictRequestRecord) -> PredictResponse:
    return PredictResponse(
        request_id=record.id, student_id=record.student_id,
        artifact_id=record.artifact_id, version=record.artifact_version,
        status=record.status.lower(), result=record.result, error=record.error,
    )


@router.post("/predict", response_model=PredictResponse)
async def predict(request: Request, response: Response) -> PredictResponse:
    manager: LeaseManager = request.app.state.services.lease_manager
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        student_id = form.get("student_id")
        image = form.get("image")
        if not isinstance(student_id, str) or not student_id.strip():
            raise HTTPException(status_code=422, detail="student_id is required")
        if len(student_id) > 128:
            raise HTTPException(status_code=422, detail="student_id is too long")
        if not isinstance(image, UploadFile):
            raise HTTPException(status_code=422, detail="image file is required")
        image_content_type = image.content_type
        if image_content_type not in {"image/jpeg", "image/png"}:
            await image.close()
            raise HTTPException(status_code=422, detail="image must be JPEG or PNG")
        limit = request.app.state.services.settings.max_predict_image_size
        image_bytes = await image.read(limit + 1)
        await image.close()
        if not image_bytes:
            raise HTTPException(status_code=422, detail="image file is empty")
        if len(image_bytes) > limit:
            raise HTTPException(status_code=413, detail=f"image exceeds {limit} bytes")
        body = PublicPredictRequest(
            student_id=student_id.strip(),
            payload={
                "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                "content_type": image_content_type,
            },
        )
    else:
        try:
            body = PublicPredictRequest.model_validate(await request.json())
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="invalid JSON predict request") from exc
    try:
        record = await manager.submit_predict(body.student_id, body.payload)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="student has no ready artifact") from exc
    response.status_code = (status.HTTP_202_ACCEPTED
                            if record.status == RequestStatus.QUEUED.value
                            else status.HTTP_200_OK)
    return response_for(record)


@router.get("/requests/{request_id}", response_model=PredictResponse)
async def get_request(request_id: str, request: Request) -> PredictResponse:
    manager: LeaseManager = request.app.state.services.lease_manager
    try:
        return response_for(await manager.get_request(request_id))
    except RequestNotFoundError as exc:
        raise HTTPException(status_code=404, detail="request not found") from exc
