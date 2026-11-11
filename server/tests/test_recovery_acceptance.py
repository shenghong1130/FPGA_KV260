from datetime import timedelta
import sqlite3

import pytest
from app.config import Settings
from app.db_models import StudentLease, Worker, utcnow
from app.worker_client import WorkerClientError
from testbed.recovery_acceptance import consistent_backup
from .conftest import upload, predict


@pytest.mark.asyncio
@pytest.mark.parametrize('idle_seconds,released',[(29.999,False),(30,True),(30.001,True)])
async def test_exact_30_second_idle_boundary(test_context,monkeypatch,idle_seconds,released):
    client,fake=test_context;services=fake.application.state.services
    await upload(client,'idle-30');assert (await predict(client,'idle-30')).json()['status']=='completed'
    now=utcnow()
    object.__setattr__(services.settings,'lease_idle_timeout_seconds',30)
    with services.database.sessions() as database:
        lease=database.get(StudentLease,'idle-30');board,token=lease.worker_id,lease.lease_id
        lease.last_activity_at=now-timedelta(seconds=idle_seconds);database.commit()
    monkeypatch.setattr('app.lease_manager.utcnow',lambda:now)
    await services.lease_manager.reap_once()
    with services.database.sessions() as database:
        lease=database.get(StudentLease,'idle-30');worker=database.get(Worker,board)
        if released:
            assert lease.state=='UNASSIGNED' and lease.lease_id is None
            assert worker.state=='IDLE' and worker.lease_id is None and not worker.fpga_ready
            assert fake.states[board]['lease_id'] is None
        else:
            assert lease.state==worker.state=='READY'
            assert lease.lease_id==worker.lease_id==fake.states[board]['lease_id']==token


@pytest.mark.asyncio
async def test_30_second_release_failure_retains_owner(test_context,monkeypatch):
    client,fake=test_context;services=fake.application.state.services
    await upload(client,'idle-30');await predict(client,'idle-30')
    now=utcnow();object.__setattr__(services.settings,'lease_idle_timeout_seconds',30)
    with services.database.sessions() as database:
        lease=database.get(StudentLease,'idle-30');board,token=lease.worker_id,lease.lease_id
        lease.last_activity_at=now-timedelta(seconds=31);database.commit()
    async def release_failed(*_):raise WorkerClientError('simulated old Worker empty-release conflict',status_code=409)
    fake.release=release_failed
    monkeypatch.setattr('app.lease_manager.utcnow',lambda:now)
    await services.lease_manager.reap_once()
    with services.database.sessions() as database:
        lease=database.get(StudentLease,'idle-30');worker=database.get(Worker,board)
        assert lease.state==worker.state=='ERROR'
        assert lease.lease_id==worker.lease_id==fake.states[board]['lease_id']==token
        assert 'empty-release conflict' in lease.error


def test_idle_30_env_overrides_legacy_timeout(monkeypatch):
    monkeypatch.setenv('LEASE_IDLE_TIMEOUT_SECONDS','30')
    monkeypatch.setenv('SESSION_IDLE_TIMEOUT_SECONDS','900')
    monkeypatch.setenv('LEASE_REAPER_INTERVAL_SECONDS','1')
    settings=Settings.from_env()
    assert settings.lease_idle_timeout_seconds==30
    assert settings.lease_reaper_interval_seconds==1


def test_online_backup_includes_uncheckpointed_wal_and_restores(tmp_path):
    source=tmp_path/'source.db';connection=sqlite3.connect(source)
    try:
        assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
        connection.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, marker TEXT)')
        connection.execute("INSERT INTO sample VALUES (1,'isolated-data')");connection.commit()
        assert source.with_name('source.db-wal').stat().st_size>0
        consistent_backup(source,tmp_path/'backup.db')
        consistent_backup(tmp_path/'backup.db',tmp_path/'restored.db')
        restored=sqlite3.connect(tmp_path/'restored.db')
        try:
            assert restored.execute('SELECT * FROM sample').fetchall()==[(1,'isolated-data')]
            assert restored.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        finally:restored.close()
        assert connection.execute('SELECT count(*) FROM sample').fetchone()[0]==1
    finally:connection.close()
