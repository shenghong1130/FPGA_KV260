"""Recovery acceptance over temporary localhost services with a simulated FPGA.

Run from server/: python -m testbed.recovery_acceptance
Never runs PYNQ, contacts production Workers, or restarts an existing service.
Includes a real 30-second idle interval and SQLite/WAL backup/restore checks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
from .design_fixture import make_bit, make_hwh
from .offline_regression import free_port
from .smoke_test import wait_for


class OfflineBackend:
    async def initialize(self):
        pass

    async def load_overlay(self, bit_path):
        assert bit_path.is_file()

    async def predict(self, payload):
        await asyncio.sleep(0.02)
        return {'ok': True, 'backend': 'SIMULATED_FPGA', 'input': payload}

    async def release(self):
        pass


def create_worker():
    # Only the script's own child processes may use this simulated app factory.
    root = Path(os.environ['KV260_RECOVERY_TEST_ROOT']).resolve()
    if root.parent != Path('/tmp') or not root.name.startswith('kv260-recovery-http-'):
        raise RuntimeError('temporary recovery test root required')
    board = os.environ['KV260_RECOVERY_TEST_BOARD']
    from worker.app.main import create_app
    return create_app(board=board, artifact_root=root/board, backend=OfflineBackend())


def rows(path, query, parameters=()):
    connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        return [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()


def consistent_backup(source, destination):
    connection = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)
    target = sqlite3.connect(destination)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:
        target.close()
        connection.close()


def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


async def run():
    root = Path(tempfile.mkdtemp(prefix='kv260-recovery-http-'))
    server = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=str(server)+os.pathsep+str(server.parent), PYTHONDONTWRITEBYTECODE='1',
        DATABASE_URL=f'sqlite:///{root/"central.db"}', ARTIFACT_ROOT=str(root/'artifacts'),
        WORKERS_CONFIG=str(root/'workers.json'), LEASE_IDLE_TIMEOUT_SECONDS='30',
        LEASE_REAPER_INTERVAL_SECONDS='0.2', LEASE_RECLAIM_GRACE_SECONDS='3600',
        HEALTH_INTERVAL_SECONDS='0.3', WORKER_CONNECT_TIMEOUT='1',
        WORKER_REQUEST_TIMEOUT='3', WORKER_DEPLOY_TIMEOUT='3',
        KV260_RECOVERY_TEST_ROOT=str(root))
    children, logs = [], []
    summary = {'hardware': 'SIMULATED; real FPGA NOT validated', 'test_root': str(root)}
    def start(module, port, name, env, factory=False):
        log=(root/f'{name}.log').open('wb');logs.append(log)
        args=[sys.executable,'-m','uvicorn',module,'--host','127.0.0.1','--port',str(port),'--log-level','warning']
        if factory:args.append('--factory')
        process=subprocess.Popen(args,cwd=server,env=env,stdout=log,stderr=log)
        children.append(process)
    try:
        config=[]
        for index in range(1,4):
            board=f'kv260{index}';port=free_port()
            config.append({'board':board,'base_url':f'http://127.0.0.1:{port}'})
            start('testbed.recovery_acceptance:create_worker',port,board,
                  dict(environment,KV260_RECOVERY_TEST_BOARD=board),True)
        (root/'workers.json').write_text(json.dumps(config))
        async with httpx.AsyncClient(trust_env=False,timeout=10) as client:
            for worker in config:await wait_for(client,worker['base_url']+'/health')
            port=free_port();base=f'http://127.0.0.1:{port}'
            start('app.main:app',port,'central',environment)
            await wait_for(client,base+'/health')
            async def upload(student,hwh):
                return await client.post(base+'/fpga/artifacts',data={'student_id':student,'password':'offline-recovery-password'},
                    files={'bit':('design.bit',make_bit()),'hwh':('design.hwh',hwh)})
            rejected=await upload('sg-student',make_hwh(1))
            assert rejected.status_code==422 and 'C_INCLUDE_SG=0' in rejected.json()['detail']
            assert not rows(root/'central.db', 'SELECT id FROM artifacts')
            summary['sg_rejected_before_deployment']=True
            print('PASS: SG rejected before deployment',flush=True)
            for student in ('a','b','c'):
                result=await upload(student,make_hwh());assert result.status_code==201
            async def predict(student):
                result=await client.post(base+'/predict',headers={'X-Student-Password':'offline-recovery-password'},
                    json={'student_id':student,'payload':{'value':student}})
                assert result.status_code==200 and result.json()['status']=='completed'
                assert result.json()['result']['backend']=='SIMULATED_FPGA'
            await asyncio.gather(*(predict(student) for student in ('a','b','c')))
            leases=rows(root/'central.db', 'SELECT * FROM student_leases')
            assert len(leases)==3 and len({x['worker_id'] for x in leases})==3 and len({x['lease_id'] for x in leases})==3
            workers=(await client.get(base+'/workers')).json()
            assert len([x for x in workers if x['state']=='ready' and x['fpga_ready']])==3
            for worker in config:
                remote=(await client.get(worker['base_url']+'/status')).json()
                owner=next(x for x in leases if x['worker_id']==worker['board'])
                assert remote['lease_id']==owner['lease_id'] and remote['artifact_id']==owner['current_artifact_id']
            summary['normal_deploy_predict_and_concurrent_ownership']=True
            summary['dashboard_workers_api_ready']=True
            dashboard=await client.get(base+'/ui/');assert dashboard.status_code==200
            javascript=await client.get(base+'/ui/dashboard.js')
            assert 'last_error' in javascript.text and 'fpga_ready' in javascript.text
            print('PASS: normal simulated deployment/prediction; 3 students own distinct Workers',flush=True)
            latest=max(utc(x['last_activity_at']) for x in leases)
            await asyncio.sleep(max(0, 29-(datetime.now(timezone.utc)-latest).total_seconds()))
            newest=max(leases,key=lambda x:x['last_activity_at'])
            before=rows(root/'central.db','SELECT state,lease_id FROM student_leases WHERE student_id=?',(newest['student_id'],))[0]
            assert before['state']=='READY' and before['lease_id']==newest['lease_id']
            print('PASS: 29 seconds idle does not release lease',flush=True)
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                after=rows(root/'central.db','SELECT * FROM student_leases')
                if all(x['state']=='UNASSIGNED' and x['lease_id'] is None for x in after):break
                await asyncio.sleep(.1)
            assert all(x['state']=='UNASSIGNED' and x['lease_id'] is None for x in after)
            elapsed={x['student_id']:(utc(x['released_at'])-utc(next(y['last_activity_at'] for y in leases if y['student_id']==x['student_id']))).total_seconds() for x in after}
            assert all(30<=value<35 for value in elapsed.values()),elapsed
            summary['actual_idle_release_seconds']=elapsed
            workers=(await client.get(base+'/workers')).json()
            assert all(x['state']=='idle' and x['lease_id'] is None and not x['fpga_ready'] for x in workers)
            for worker in config:
                remote=(await client.get(worker['base_url']+'/status')).json()
                assert remote['lease_id'] is None and not remote['fpga_ready']
            assert not rows(root/'central.db', 'SELECT board FROM workers w WHERE session_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM student_leases s WHERE s.lease_id=w.session_id AND s.worker_id=w.board)')
            assert not rows(root/'central.db', 'SELECT student_id FROM student_leases s WHERE lease_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM workers w WHERE w.session_id=s.lease_id AND w.board=s.worker_id)')
            summary['dashboard_workers_api_idle_and_no_orphans']=True
            print('PASS: 30-second automatic idle release; all three parties clear, Workers IDLE',elapsed,flush=True)
            assert rows(root/'central.db','PRAGMA journal_mode')[0]['journal_mode']=='wal'
            consistent_backup(root/'central.db',root/'backup.db')
            consistent_backup(root/'backup.db',root/'restored.db')
            for table in ('workers','student_leases','predict_requests','artifacts','audit_events'):
                assert rows(root/'central.db',f'SELECT count(*) AS n FROM {table}')==rows(root/'restored.db',f'SELECT count(*) AS n FROM {table}')
            for worker in config:
                source=root/worker['board']/'fencing.sqlite'
                copy=root/(worker['board']+'-fencing-backup.sqlite')
                consistent_backup(source,copy)
                assert rows(copy,'SELECT count(*) AS n FROM owner')[0]['n']==0
                assert rows(copy,'SELECT count(*) AS n FROM retired_leases')[0]['n']>=1
            summary['central_wal_backup_restore_and_worker_fencing_backup']=True
            print('PASS: isolated SQLite WAL backup/restore and persistent Worker journal backups',flush=True)
            summary['result']='PASS'
    finally:
        for child in children:
            if child.poll() is None:child.terminate()
        for child in children:
            try:await asyncio.to_thread(child.wait,8)
            except subprocess.TimeoutExpired:child.kill();await asyncio.to_thread(child.wait)
        for log in logs:log.close()
        (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
        print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    asyncio.run(run())
