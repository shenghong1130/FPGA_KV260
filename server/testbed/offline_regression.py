"""Isolated localhost HTTP integration tests. Never contacts real Workers.

Run from server/: python -m testbed.offline_regression
All child services, SQLite and artifacts belong to this temporary test cluster.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import httpx

from .design_fixture import make_bit, make_hwh
from . import smoke_test as smoke
from . import multi_student_persistence_test as multi


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def run() -> None:
    root = Path(tempfile.mkdtemp(prefix='kv260-http-regression-'))
    server = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(server) + os.pathsep + str(server.parent),
        PYTHONDONTWRITEBYTECODE='1', DATABASE_URL=f'sqlite:///{root / "isolated.db"}',
        ARTIFACT_ROOT=str(root/'artifacts'), WORKERS_CONFIG=str(root/'workers.json'),
        LEASE_IDLE_TIMEOUT_SECONDS='3600', LEASE_RECLAIM_GRACE_SECONDS='1',
        LEASE_REAPER_INTERVAL_SECONDS='0.1', HEALTH_INTERVAL_SECONDS='0.2',
        WORKER_CONNECT_TIMEOUT='0.5', WORKER_REQUEST_TIMEOUT='2', WORKER_DEPLOY_TIMEOUT='3')
    children = []
    handles = []
    def start(module,port,env,name):
        log = (root/f'{name}.log').open('ab'); handles.append(log)
        child = subprocess.Popen([sys.executable,'-m','uvicorn',module,'--host','127.0.0.1',
            '--port',str(port),'--log-level','warning'],cwd=server,env=env,stdout=log,stderr=log)
        children.append(child)
        return child
    async def stop(child):
        if child.poll() is None:
            child.terminate()
            try:
                await asyncio.to_thread(child.wait, 10)
            except subprocess.TimeoutExpired:
                child.kill(); await asyncio.to_thread(child.wait)
    try:
        entries=[]
        for index in range(1,4):
            port=free_port()
            env=dict(environment,MOCK_BOARD=f'mock-kv260{index}')
            start('testbed.mock_worker:app',port,env,f'worker{index}')
            entries.append({'board':f'mock-kv260{index}','base_url':f'http://127.0.0.1:{port}'})
        (root/'workers.json').write_text(json.dumps(entries))
        async with httpx.AsyncClient(trust_env=False,timeout=10) as client:
            for entry in entries:await smoke.wait_for(client,entry['base_url']+'/health')
            port=free_port();base=f'http://127.0.0.1:{port}'
            central=start('app.main:app',port,environment,'central')
            await smoke.wait_for(client,base+'/health')
            await smoke.run(base,root/'workers.json')
            multi.STATE_PATH=root/'multi_student_test_state.json'
            await multi.phase0_health(client,base)
            runs=await multi.phase1_artifacts(client,base,make_bit(),make_hwh(),False)
            # Small PNG, valid image; mock worker returns a deterministic result.
            import io
            from PIL import Image
            output=io.BytesIO();Image.new('RGB',(8,8)).save(output,format='PNG')
            await multi.phase2_submit(client,base,runs,output.getvalue())
            await multi.phase3_poll(client,base,runs,30)
            await multi.check_audit(client,base,runs,persistence=False)
            async def restart():
                nonlocal central
                print('Restarting ONLY this harness-owned temporary Central',flush=True)
                await stop(central)
                central=start('app.main:app',port,environment,'central')
            await multi.phase5_restart(client,base,runs,30,restart=restart)
            multi.print_result(runs,True,restart_checked=True)
            print('ISOLATED HTTP REGRESSION: PASS',flush=True)
    finally:
        await asyncio.gather(*(stop(child) for child in children))
        for handle in handles:handle.close()
        print(f'Test artifacts and logs: {root}',flush=True)


if __name__=='__main__':
    asyncio.run(run())
