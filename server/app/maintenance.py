"""Auditable maintenance operations, invoked only with scheduling paused."""
from __future__ import annotations

import uuid
from sqlalchemy import select
from .db_models import AuditEvent, PredictRequestRecord, RequestStatus, StudentLease, LeaseStatus, utcnow


def cancel_unstarted_requests(sessions, request_ids: list[str], *, maintenance_id: str, reason: str) -> list[str]:
    """Cancel exact, never-dispatched IDs atomically with their audit events.

    Started or ambiguously dispatched work is deliberately refused. It requires
    separate remote-operation/DMA safety evidence, not a database-only cancel.
    Existing Lease identities, credentials, Artifacts and results are preserved.
    """
    if not maintenance_id.strip() or not reason.strip():
        raise ValueError('maintenance ID and reason required')
    cancelled=[]
    with sessions() as database, database.begin():
        records=[]
        for request_id in dict.fromkeys(request_ids):
            record=database.get(PredictRequestRecord,request_id)
            if record is None:raise LookupError(request_id)
            if record.status==RequestStatus.CANCELLED.value:continue
            if (record.status!=RequestStatus.QUEUED.value or record.started_at is not None or
                    record.worker_id is not None or record.deploy_attempts!=0):
                raise ValueError(f'{request_id}: dispatched/started work requires verified remote DMA stop')
            records.append(record)
        for record in records:
            record.status=RequestStatus.CANCELLED.value
            record.error=reason
            record.completed_at=utcnow()
            database.add(AuditEvent(id=f'evt_{uuid.uuid4().hex}',event_type='REQUEST_CANCELLED',
                level='WARNING',actor_type='administrator',actor_id=maintenance_id,
                student_id=record.student_id,artifact_id=record.artifact_id,request_id=record.id,
                message=reason,details={'maintenance_id':maintenance_id,'previous_status':'QUEUED',
                    'never_dispatched':True,'deploy_attempts':record.deploy_attempts}))
            cancelled.append(record.id)
        database.flush()
        for student_id in {record.student_id for record in records}:
            lease=database.get(StudentLease,student_id)
            pending=database.scalar(select(PredictRequestRecord.id).where(
                PredictRequestRecord.student_id==student_id,
                PredictRequestRecord.status.in_(['QUEUED','RUNNING'])).limit(1))
            if (lease and lease.state==LeaseStatus.QUEUED.value and not lease.lease_id and
                    not lease.worker_id and pending is None):
                lease.state=LeaseStatus.UNASSIGNED.value
    return cancelled
