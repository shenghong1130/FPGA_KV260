from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.db_models import Artifact, StudentLease, Worker, PredictRequestRecord
from app.hardware_validation import validate_design, HardwareValidationError
from app.worker_client import WorkerClientError, check_response, WorkerClient
from testbed.design_fixture import make_bit, make_hwh
from .conftest import upload, predict, TEST_PASSWORD, password_headers


def test_valid_simple_dma():
    validate_design(make_bit(), make_hwh())


@pytest.mark.parametrize('bit,hwh,message', [
    (make_bit(), make_hwh(1), 'C_INCLUDE_SG=0'),
    (make_bit(part=b'xczu3eg-sbva484-1-i'), make_hwh(), 'BIT target'),
    (b'raw-bit', make_hwh(), 'BIT preamble'),
    (make_bit()[:-1], make_hwh(), 'payload'),
    (make_bit(), b'<SYSTEM/>', 'axi_dma_0'),
    (make_bit(), b'not-xml', 'invalid HWH XML'),
    (make_bit(), make_hwh().replace(b'C_INCLUDE_MM2S" VALUE="1', b'C_INCLUDE_MM2S" VALUE="0'), 'MM2S'),
    (make_bit(), make_hwh().replace(b'C_INCLUDE_S2MM" VALUE="1', b'C_INCLUDE_S2MM" VALUE="0'), 'S2MM'),
    (make_bit(), make_hwh().replace(b'xck26', b'xczu3eg'), 'HWH target'),
    (make_bit(), make_hwh().replace(b'C_SG_LENGTH_WIDTH" VALUE="14', b'C_SG_LENGTH_WIDTH" VALUE="13'), '9408'),
    (make_bit(), make_hwh().replace(b'BUSNAME="net_S_AXI_LITE"', b'BUSNAME=""'), 'interface S_AXI_LITE'),
    (make_bit(), make_hwh().replace(b'C_INCLUDE_SG" VALUE="0', b'C_INCLUDE_SG" VALUE="x'), 'missing/invalid'),
    (make_bit(), make_hwh().replace(b'C_M_AXI_S2MM_DATA_WIDTH" VALUE="32', b'C_M_AXI_S2MM_DATA_WIDTH" VALUE="24'), 'data width'),
])
def test_bad_design(bit, hwh, message):
    with pytest.raises(HardwareValidationError, match=message):
        validate_design(bit, hwh)


@pytest.mark.asyncio
async def test_sg_upload_never_reaches_workers(test_context):
    client, fake = test_context
    result = await client.post('/fpga/artifacts', data={'student_id':'bad', 'password':TEST_PASSWORD},
        files={'bit':('design.bit',make_bit()), 'hwh':('design.hwh',make_hwh(1))})
    assert result.status_code == 422
    assert 'Scatter Gather' in result.json()['detail']
    assert fake.deploy_counts == {}
    services = fake.application.state.services
    with services.database.sessions() as db:
        assert list(db.scalars(select(Artifact))) == []
    assert not list(services.settings.artifact_root.iterdir())
    await upload(client, 'other')
    assert (await predict(client, 'other')).json()['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('permanent,retryable,attempts', [(True,False,1),(False,True,3),(False,False,1)])
async def test_deploy_error_bounded_and_owned(test_context, permanent, retryable, attempts):
    client, fake = test_context
    artifact = await upload(client, 'bad')
    services = fake.application.state.services
    calls = []
    original = fake.deploy
    async def failed(worker, lease_id, design):
        calls.append((worker.board,lease_id))
        # Fail after the remote Worker accepted ownership (ambiguous timeout).
        await original(worker,lease_id,design)
        raise WorkerClientError('underlying DMA error', permanent=permanent, retryable=retryable)
    fake.deploy = failed
    result = (await predict(client,'bad')).json()
    assert result['status'] == 'failed'
    assert 'underlying DMA error' in result['error']
    assert len(calls) == attempts and len(set(calls)) == 1
    with services.database.sessions() as db:
        lease = db.get(StudentLease,'bad'); worker = db.get(Worker,lease.worker_id)
        assert lease.lease_id == worker.lease_id == fake.states[worker.board]['lease_id']
        assert lease.state == worker.state == 'ERROR'
        assert db.get(Artifact,artifact['artifact_id']).status == ('FAILED' if permanent else 'READY')
    for _ in range(5):
        await services.lease_manager.reap_once()
        await asyncio.sleep(0)
    assert len(calls) == attempts
    fake.deploy = original
    await upload(client,'good')
    assert (await predict(client,'good')).json()['status'] == 'completed'


@pytest.mark.asyncio
async def test_existing_sg_artifact_revalidated_before_worker_io(test_context):
    client,fake = test_context
    art = await upload(client,'legacy')
    services = fake.application.state.services
    with services.database.sessions() as db:
        path = Path(db.get(Artifact,art['artifact_id']).hwh_path)
    path.write_bytes(make_hwh(1))
    import hashlib
    with services.database.sessions() as db:
        db.get(Artifact,art['artifact_id']).hwh_sha256=hashlib.sha256(make_hwh(1)).hexdigest()
        db.commit()
    result = (await predict(client,'legacy')).json()
    assert result['status'] == 'failed' and 'C_INCLUDE_SG' in result['error']
    assert fake.deploy_counts == {}


@pytest.mark.asyncio
async def test_release_failure_preserves_three_way_ownership_and_pending(test_context):
    client,fake = test_context
    await upload(client,'a'); await predict(client,'a')
    services = fake.application.state.services
    with services.database.sessions() as db:
        lease = db.get(StudentLease,'a'); board,token = lease.worker_id,lease.lease_id
    fake.fail_predict_for.add(token)
    assert (await predict(client,'a')).json()['status'] == 'failed'
    original = fake.release
    async def broken(*args):
        raise WorkerClientError('release offline')
    fake.release = broken
    for _ in range(3):
        await services.lease_manager.reap_once()
        await services.worker_registry._check_worker(board)
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').lease_id == db.get(Worker,board).lease_id == fake.states[board]['lease_id'] == token
    queued = (await predict(client,'a')).json()
    assert queued['status'] == 'queued'
    fake.fail_predict_for.clear(); fake.release = original
    await services.lease_manager.reap_once()
    for _ in range(100):
        item = (await client.get('/requests/'+queued['request_id'],headers=password_headers())).json()
        if item['status']=='completed': break
        await asyncio.sleep(.01)
    assert item['status']=='completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_ready',[False,True])
async def test_unknown_owner_readiness_refreshes_but_stays_quarantined(test_context,remote_ready):
    _,fake = test_context; services=fake.application.state.services
    async def status(worker):
        return {'lease_id':'orphan','artifact_id':'unknown','fpga_ready':remote_ready}
    fake.status=status
    await services.worker_registry._check_worker('mock-kv2601')
    with services.database.sessions() as db:
        worker=db.get(Worker,'mock-kv2601')
        assert bool(worker.fpga_ready)==remote_ready and worker.state=='ERROR'
        assert worker.lease_id=='orphan'


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['DEPLOYING','BUSY','RELEASING','RESERVED'])
async def test_restart_inflight_keeps_owner_until_release(test_context,stage):
    client,fake=test_context; services=fake.application.state.services
    await upload(client,'a'); await predict(client,'a')
    with services.database.sessions() as db:
        lease=db.get(StudentLease,'a'); token,board=lease.lease_id,lease.worker_id
        lease.state=stage; db.get(Worker,board).state='BUSY' if stage=='BUSY' else 'DEPLOYING'
        db.commit()
    await services.lease_manager.recover_requests()
    await services.worker_registry.recover()
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').lease_id==db.get(Worker,board).lease_id==token
        assert db.get(StudentLease,'a').state in ('LOST','ERROR')
        assert db.get(Worker,board).state=='ERROR'
    await services.lease_manager.reap_once()
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').lease_id is None
        assert db.get(Worker,board).lease_id is None
    assert fake.states[board]['lease_id'] is None


@pytest.mark.parametrize('status,detail,permanent,retryable',[
    (422,'bad interface',True,False), (500,'axi_dma_0 must use Simple DMA',True,False),
    (503,'maintenance',False,True), (409,'foreign owner',False,False), (500,'DMA fault',False,False)])
def test_http_detail_and_classification(status,detail,permanent,retryable):
    with pytest.raises(WorkerClientError) as error:
        check_response(httpx.Response(status,json={'detail':detail}))
    assert detail in str(error.value)
    assert error.value.permanent==permanent and error.value.retryable==retryable


@pytest.mark.asyncio
async def test_stale_status_cannot_replace_new_reservation(test_context):
    client,fake=test_context; services=fake.application.state.services
    entered,resume=asyncio.Event(),asyncio.Event()
    async def stale(worker):
        entered.set(); await resume.wait()
        return {'lease_id':None,'artifact_id':None,'fpga_ready':False}
    fake.status=stale
    check=asyncio.create_task(services.worker_registry._check_worker('mock-kv2601'))
    await entered.wait()
    # Directly reserve this board while an old IDLE observation is in flight.
    with services.database.sessions() as db:
        worker=db.get(Worker,'mock-kv2601'); worker.state='RESERVED';worker.lease_id='new'
        db.commit()
    resume.set();await check
    with services.database.sessions() as db:
        assert db.get(Worker,'mock-kv2601').state=='RESERVED'
        assert db.get(Worker,'mock-kv2601').lease_id=='new'


@pytest.mark.asyncio
async def test_real_worker_http_failure_and_restart_recovery(test_context,tmp_path):
    # Real Worker routes/state and real WorkerClient over ASGI; FPGA backend only
    # is simulated. This covers HTTP errors and all three ownership copies.
    from worker.app.main import create_app
    from worker.tests.test_worker import FakeBackend
    client,fake=test_context;services=fake.application.state.services
    backend=FakeBackend()
    app=create_app(board='kv2601',artifact_root=tmp_path/'worker',backend=backend)
    async with app.router.lifespan_context(app):
        real=WorkerClient(services.settings)
        await real.http.aclose()
        real.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://mock1')
        services.lease_manager.client=real;services.worker_registry.client=real
        with services.database.sessions() as db:
            for board in ('mock-kv2602','mock-kv2603'):db.get(Worker,board).state='OFFLINE'
            db.commit()
        try:
            await upload(client,'a')
            original=backend.load_overlay
            async def broken(_):raise RuntimeError('specific Overlay fault')
            backend.load_overlay=broken
            result=(await predict(client,'a')).json()
            assert result['status']=='failed' and 'specific Overlay fault' in result['error']
            with services.database.sessions() as db:
                lease=db.get(StudentLease,'a');token=lease.lease_id
                assert lease.state=='ERROR'
                assert token==db.get(Worker,'mock-kv2601').lease_id==app.state.worker.current_lease_id
            await services.lease_manager.reap_once()
            assert app.state.worker.current_lease_id is None
            backend.load_overlay=original
            assert (await predict(client,'a')).json()['status']=='completed'
            # Runtime DMA fault invalidates readiness, keeps ownership.
            failed=await services.lease_manager.submit_predict('a',{'hardware_error':True})
            assert failed.status=='FAILED' and 'DMA failed' in failed.error
            assert not app.state.worker.fpga_ready
            await services.worker_registry._check_worker('mock-kv2601')
            with services.database.sessions() as db:
                lease=db.get(StudentLease,'a')
                assert lease.lease_id==db.get(Worker,'mock-kv2601').lease_id==app.state.worker.current_lease_id
                assert not db.get(Worker,'mock-kv2601').fpga_ready
            await services.lease_manager.reap_once()
            assert app.state.worker.current_lease_id is None
        finally:
            await real.close()
            services.lease_manager.client=fake;services.worker_registry.client=fake


@pytest.mark.asyncio
async def test_invalid_predict_payload_fails_request_without_quarantining_worker(test_context):
    client,fake=test_context;services=fake.application.state.services
    await upload(client,'a');await predict(client,'a')
    with services.database.sessions() as db:
        lease=db.get(StudentLease,'a');token,board=lease.lease_id,lease.worker_id
    original=fake.predict
    async def bad(*args):raise WorkerClientError('invalid image',status_code=422,permanent=True)
    fake.predict=bad
    item=(await predict(client,'a')).json()
    assert item['status']=='failed' and 'invalid image' in item['error']
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').state==db.get(Worker,board).state=='READY'
        assert db.get(StudentLease,'a').lease_id==token
        assert db.get(Worker,board).fpga_ready
    fake.predict=original
    assert (await predict(client,'a')).json()['status']=='completed'
    assert fake.deploy_counts[token]==1


@pytest.mark.asyncio
async def test_retry_budget_survives_lost_lease_and_recovery(test_context):
    client,fake=test_context;services=fake.application.state.services
    await upload(client,'a')
    original=fake.deploy;calls=[]
    async def stale(worker,token,artifact):
        result=await original(worker,token,artifact);calls.append(token)
        with services.database.sessions() as db:
            db.get(StudentLease,'a').state='LOST'
            db.get(Worker,worker.board).state='OFFLINE'
            db.commit()
        return result
    fake.deploy=stale
    request=(await predict(client,'a')).json()
    for _ in range(8):
        await services.lease_manager.reap_once()
        await asyncio.sleep(.03)
    result=(await client.get('/requests/'+request['request_id'],headers=password_headers())).json()
    assert len(calls)==3
    assert result['status']=='failed' and 'budget exhausted' in result['error']
    with services.database.sessions() as db:
        assert db.get(PredictRequestRecord,request['request_id']).deploy_attempts==3
        assert db.get(StudentLease,'a').lease_id is None


def test_additive_attempt_migration_and_wal(tmp_path):
    import sqlite3
    from app.database import Database
    path=tmp_path/'old.db'
    connection=sqlite3.connect(path)
    connection.execute('CREATE TABLE predict_requests (id TEXT PRIMARY KEY, student_id TEXT, artifact_id TEXT, artifact_version TEXT, worker_id TEXT, status TEXT, payload JSON, result JSON, created_at DATETIME, started_at DATETIME, completed_at DATETIME, error TEXT)')
    connection.execute("INSERT INTO predict_requests (id,status,payload) VALUES ('old','COMPLETED','{}')")
    connection.commit();connection.close()
    database=Database(f'sqlite:///{path}');database.initialize();database.initialize()
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT deploy_attempts,status FROM predict_requests WHERE id='old'").one()==(0,'COMPLETED')
        assert connection.exec_driver_sql('PRAGMA journal_mode').scalar()=='wal'
        assert connection.exec_driver_sql('PRAGMA synchronous').scalar()==2
    database.close()


@pytest.mark.asyncio
async def test_late_predict_success_cannot_revive_offline_lease(test_context):
    client,fake=test_context;services=fake.application.state.services
    await upload(client,'a');await predict(client,'a')
    entered,resume=asyncio.Event(),asyncio.Event();original=fake.predict
    async def slow(worker,token,payload):
        result=await original(worker,token,payload)
        entered.set();await resume.wait();return result
    fake.predict=slow
    operation=asyncio.create_task(predict(client,'a'));await entered.wait()
    with services.database.sessions() as db:
        lease=db.get(StudentLease,'a');board,token=lease.worker_id,lease.lease_id
    async def down(_):raise WorkerClientError('offline')
    fake.health=down
    await services.worker_registry._check_worker(board,recovery=True)
    resume.set();item=(await operation).json()
    assert item['status']=='failed' and 'offline' in item['error']
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').state=='LOST'
        assert db.get(StudentLease,'a').lease_id==db.get(Worker,board).lease_id==token
    async def up(_):return {'ok':True}
    fake.health=up
    await services.worker_registry._check_worker(board)
    with services.database.sessions() as db:
        assert db.get(Worker,board).state=='ERROR'
    await services.lease_manager.reap_once()
    assert fake.states[board]['lease_id'] is None


@pytest.mark.asyncio
async def test_lost_release_response_is_reconciled_by_idempotent_retry(test_context):
    client,fake=test_context;services=fake.application.state.services
    await upload(client,'a');await predict(client,'a')
    with services.database.sessions() as db:
        lease=db.get(StudentLease,'a');board,token=lease.worker_id,lease.lease_id
    original=fake.release
    async def lost(worker,lease_id):
        await original(worker,lease_id)
        raise WorkerClientError('release acknowledgement lost',retryable=True)
    fake.release=lost
    assert not await services.lease_manager.release_student('a','test')
    assert fake.states[board]['lease_id'] is None
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').lease_id==db.get(Worker,board).lease_id==token
    # A status poll must preserve the pending reconciliation identity.
    await services.worker_registry._check_worker(board)
    fake.release=original
    await services.lease_manager.reap_once()
    with services.database.sessions() as db:
        assert db.get(StudentLease,'a').lease_id is None
        assert db.get(Worker,board).state=='IDLE'
