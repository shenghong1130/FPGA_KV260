from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select,func

from app.db_models import Artifact,AuditEvent,PredictRequestRecord,StudentLease,Worker,utcnow
from app.maintenance import cancel_unstarted_requests
from app.main import create_app
from app.worker_client import WorkerClientError
from testbed.design_fixture import make_hwh
from .conftest import upload,predict,password_headers,FakeWorkerClient


@pytest.mark.asyncio
async def test_cancellation_is_atomic_audited_idempotent_and_preserves_ownership(test_context):
    client,fake=test_context;services=fake.application.state.services
    artifact=await upload(client,'student')
    with services.database.sessions() as db:
        db.add(StudentLease(student_id='student',state='LOST',worker_id='mock-kv2601',lease_id='old-lease'))
        db.add(PredictRequestRecord(id='queued',student_id='student',artifact_id=artifact['artifact_id'],artifact_version='v1',status='QUEUED',payload={'original':True}))
        db.commit()
    ids=cancel_unstarted_requests(services.database.sessions,['queued'],maintenance_id='test-maintenance',reason='authorized maintenance')
    assert ids==['queued']
    assert cancel_unstarted_requests(services.database.sessions,['queued'],maintenance_id='test-maintenance',reason='authorized maintenance')==[]
    with services.database.sessions() as db:
        record=db.get(PredictRequestRecord,'queued')
        assert record.status=='CANCELLED' and record.payload=={'original':True} and record.completed_at is not None
        assert db.get(StudentLease,'student').lease_id=='old-lease'
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type=='REQUEST_CANCELLED'))==1
        assert db.get(Artifact,artifact['artifact_id']) is not None
    item=(await client.get('/requests/queued',headers=password_headers())).json()
    assert item['status']=='cancelled'
    summary=(await client.get('/students/student/status',headers=password_headers())).json()
    assert summary['cancelled_requests']==1 and summary['total_requests']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('status,attempts,worker',[('RUNNING',0,None),('QUEUED',1,None),('QUEUED',0,'mock-kv2601')])
async def test_dispatched_cancellation_refused_without_dma_evidence(test_context,status,attempts,worker):
    client,fake=test_context;services=fake.application.state.services
    artifact=await upload(client,'student')
    with services.database.sessions() as db:
        for key,state,count,board in [('safe','QUEUED',0,None),('unsafe',status,attempts,worker)]:
            db.add(PredictRequestRecord(id=key,student_id='student',artifact_id=artifact['artifact_id'],artifact_version='v1',status=state,payload={},deploy_attempts=count,worker_id=board))
        db.commit()
    with pytest.raises(ValueError,match='DMA stop'):
        cancel_unstarted_requests(services.database.sessions,['safe','unsafe'],maintenance_id='test',reason='maintenance')
    with services.database.sessions() as db:
        assert db.get(PredictRequestRecord,'safe').status=='QUEUED'
        assert db.get(PredictRequestRecord,'unsafe').status==status
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type=='REQUEST_CANCELLED'))==0


@pytest.mark.asyncio
async def test_legacy_invalid_artifact_never_reserves_worker(test_context):
    client,fake=test_context;services=fake.application.state.services
    artifact=await upload(client,'bad')
    with services.database.sessions() as db:
        row=db.get(Artifact,artifact['artifact_id']);Path(row.hwh_path).write_bytes(make_hwh(1));row.hwh_sha256=hashlib.sha256(make_hwh(1)).hexdigest();db.commit()
    result=(await predict(client,'bad')).json()
    assert result['status']=='failed' and 'C_INCLUDE_SG=0' in result['error']
    assert fake.deploy_counts=={}
    with services.database.sessions() as db:
        assert all(w.state=='IDLE' and w.lease_id is None for w in db.scalars(select(Worker)))
        assert db.get(StudentLease,'bad').lease_id is None
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type=='WORKER_ASSIGNED'))==0
    await upload(client,'good');assert (await predict(client,'good')).json()['status']=='completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('status_code,budget',[(409,1),(503,3),(None,3)])
async def test_release_retry_is_persisted_bounded_and_backed_off(test_context,monkeypatch,status_code,budget):
    client,fake=test_context;services=fake.application.state.services
    await upload(client,'a');await predict(client,'a')
    with services.database.sessions() as db:
        row=db.get(StudentLease,'a');token,board=row.lease_id,row.worker_id
    calls=[]
    async def broken(*args):calls.append(args);raise WorkerClientError('release unavailable',status_code=status_code)
    fake.release=broken
    clock=utcnow();monkeypatch.setattr('app.lease_manager.utcnow',lambda:clock)
    assert not await services.lease_manager.release_student('a','test release')
    for _ in range(5):await services.lease_manager.reap_once()
    assert len(calls)==1
    for step in range(1,8):
        monkeypatch.setattr('app.lease_manager.utcnow',lambda step=step:clock+timedelta(seconds=10*step))
        await services.lease_manager.reap_once()
    assert len(calls)==budget
    with services.database.sessions() as db:
        row=db.get(StudentLease,'a')
        assert row.release_attempts==budget and row.release_blocked
        assert row.lease_id==db.get(Worker,board).lease_id==fake.states[board]['lease_id']==token
        assert db.get(Worker,board).state=='ERROR'
    # Fresh manager object (Central restart) uses the same persistent budget.
    from app.lease_manager import LeaseManager
    fresh=LeaseManager(services.database.sessions,services.scheduler,fake,services.settings,services.audit)
    for _ in range(3):await fresh.reap_once()
    assert len(calls)==budget
    assert not await fresh.authorize_release_retry('a','wrong-token','operator')
    assert await fresh.authorize_release_retry('a',token,'operator verified Worker repaired')
    fake.release=FakeWorkerClient.release.__get__(fake,FakeWorkerClient)
    await fresh.reap_once()
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').lease_id is None and db.get(Worker,board).state=='IDLE'


@pytest.mark.asyncio
async def test_maintenance_mode_blocks_writes_preserves_queued_and_never_sends_worker_mutations(test_context,tmp_path):
    _,fake=test_context;settings=replace(fake.application.state.services.settings,maintenance_mode=True,
        database_url=f'sqlite:///{tmp_path/"maintenance.db"}',artifact_root=tmp_path/'maintenance-artifacts')
    probe=FakeWorkerClient()
    async def forbidden(*args):raise AssertionError('maintenance must never mutate Worker')
    probe.deploy=forbidden;probe.predict=forbidden;probe.release=forbidden
    app=create_app(settings,probe)
    async with app.router.lifespan_context(app):
        assert app.state.services.lease_manager.allocator_task is None
        assert app.state.services.lease_manager.reaper_task is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://maintenance') as client:
            for path in ('/predict','/fpga/artifacts','/workers/mock-kv2601/release'):
                assert (await client.post(path,json={})).status_code==503
            health=(await client.get('/health')).json()
            assert health['maintenance_mode'] is True
            assert (await client.get('/ui/')).status_code==200
            await asyncio.sleep(.01)
        with app.state.services.database.sessions() as db:
            assert db.scalar(select(func.count()).select_from(PredictRequestRecord))==0


def test_fpga_execution_error_classification():
    from app.worker_client import check_response
    with pytest.raises(WorkerClientError) as result:
        check_response(httpx.Response(500,json={'detail':'receive DMA timeout'},headers={'X-KV260-Error-Code':'FPGA_EXECUTION_FAILED'}))
    assert result.value.permanent and not result.value.retryable

@pytest.mark.asyncio
async def test_dma_execution_failure_quarantines_artifact_and_safe_worker_recovers(test_context):
    client,fake=test_context;services=fake.application.state.services
    artifact=await upload(client,'dma-bad')
    original=fake.predict
    async def fail(worker,lease_id,payload):
        with services.database.sessions() as db:
            db.add(PredictRequestRecord(id='same-design-pending',student_id='dma-bad',artifact_id=artifact['artifact_id'],artifact_version='v1',status='QUEUED',payload={}))
            db.commit()
        raise WorkerClientError('[FPGA_EXECUTION_FAILED] receive DMA timeout',permanent=True,status_code=500)
    fake.predict=fail
    result=(await predict(client,'dma-bad')).json()
    assert result['status']=='failed'
    with services.database.sessions() as db:
        lease=db.get(StudentLease,'dma-bad');board=lease.worker_id;token=lease.lease_id
        assert lease.state=='ERROR' and db.get(Worker,board).lease_id==token
        assert db.get(Artifact,artifact['artifact_id']).status=='FAILED'
        assert db.get(PredictRequestRecord,'same-design-pending').status=='FAILED'
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type=='ARTIFACT_QUARANTINED'))==1
    await services.lease_manager.reap_once()
    with services.database.sessions() as db:
        assert db.get(StudentLease,'dma-bad').lease_id is None
        assert db.get(Worker,board).state=='IDLE' and db.get(Worker,board).lease_id is None
    assert (await predict(client,'dma-bad')).status_code==404
    assert len(fake.deploy_counts)==1
    fake.predict=original
    await upload(client,'other')
    assert (await predict(client,'other')).json()['status']=='completed'
