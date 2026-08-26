from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

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
async def predict(body: PublicPredictRequest, request: Request,
                  response: Response) -> PredictResponse:
    manager: LeaseManager = request.app.state.services.lease_manager
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
