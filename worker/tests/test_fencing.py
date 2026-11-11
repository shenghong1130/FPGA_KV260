from __future__ import annotations
import asyncio
from pathlib import Path
import pytest

from worker.app.state import WorkerState, WorkerConflictError
from worker.app.hardware_validation import HardwareValidationError, validate_design
from .test_worker import FakeBackend
from .design_fixture import make_bit, make_hwh
import hashlib

async def deploy(state, lease='lease_one', artifact='art_one'):
    bit,hwh=make_bit(),make_hwh()
    return await state.deploy(lease,artifact,hashlib.sha256(bit).hexdigest(),hashlib.sha256(hwh).hexdigest(),bit,hwh)


@pytest.mark.asyncio
async def test_retired_lease_stays_fenced_across_restart(tmp_path):
    state=WorkerState('kv2601',tmp_path,FakeBackend());await state.initialize()
    await deploy(state);await state.release('lease_one')
    # A duplicate release is acknowledged after the first response was lost.
    assert (await state.release('lease_one'))['ok']
    restarted=WorkerState('kv2601',tmp_path,FakeBackend());await restarted.initialize()
    with pytest.raises(WorkerConflictError,match='retired'):
        await deploy(restarted)
    await deploy(restarted,'lease_two','art_two')
    with pytest.raises(WorkerConflictError):await restarted.release('lease_one')
    assert restarted.current_lease_id=='lease_two' and restarted.fpga_ready


@pytest.mark.asyncio
async def test_failed_deploy_persists_owner_for_recovery(tmp_path):
    backend=FakeBackend()
    async def broken(_):raise RuntimeError('overlay failed')
    backend.load_overlay=broken
    state=WorkerState('kv2601',tmp_path,backend);await state.initialize()
    with pytest.raises(RuntimeError):await deploy(state)
    assert state.current_lease_id=='lease_one' and not state.fpga_ready
    restarted=WorkerState('kv2601',tmp_path,FakeBackend());await restarted.initialize()
    assert restarted.current_lease_id=='lease_one' and not restarted.fpga_ready
    with pytest.raises(WorkerConflictError):await deploy(restarted,'lease_two')
    # A restarted process has no DMA handles. An empty runtime is insufficient
    # evidence that the previous PL stopped writing memory.
    with pytest.raises(WorkerConflictError, match='hardware recovery'):
        await restarted.release('lease_one')
    assert restarted.current_lease_id == 'lease_one'


@pytest.mark.asyncio
async def test_failed_release_fences_deploy_but_retains_owner(tmp_path):
    backend=FakeBackend();state=WorkerState('kv2601',tmp_path,backend);await state.initialize()
    await deploy(state)
    original=backend.release
    async def broken():raise RuntimeError('cleanup failed')
    backend.release=broken
    with pytest.raises(RuntimeError):await state.release('lease_one')
    assert state.current_lease_id=='lease_one' and not state.fpga_ready
    with pytest.raises(WorkerConflictError,match='retired'):await deploy(state)
    backend.release=original
    await state.release('lease_one')
    assert state.current_lease_id is None


@pytest.mark.asyncio
async def test_superseded_design_cannot_overwrite_new_overlay(tmp_path):
    state=WorkerState('kv2601',tmp_path,FakeBackend());await state.initialize()
    await deploy(state);await deploy(state,artifact='art_two')
    with pytest.raises(WorkerConflictError,match='superseded'):await deploy(state)
    assert state.current_artifact_id=='art_two'


@pytest.mark.asyncio
async def test_cancelled_hardware_call_holds_lock_until_actual_completion(tmp_path):
    entered,resume=asyncio.Event(),asyncio.Event()
    backend=FakeBackend()
    async def blocked(_):entered.set();await resume.wait()
    backend.load_overlay=blocked
    state=WorkerState('kv2601',tmp_path,backend);await state.initialize()
    operation=asyncio.create_task(deploy(state));await entered.wait();operation.cancel()
    release=asyncio.create_task(state.release('lease_one'))
    await asyncio.sleep(.01)
    assert not release.done() and state.lock.locked()
    resume.set()
    with pytest.raises(asyncio.CancelledError):await operation
    assert (await release)['ok']
    assert state.current_lease_id is None


def test_server_and_worker_validate_same_abi():
    root=Path(__file__).resolve().parents[2]
    assert (root/'server/app/hardware_validation.py').read_bytes()==(root/'worker/app/hardware_validation.py').read_bytes()
    validate_design(make_bit(),make_hwh())
    with pytest.raises(HardwareValidationError,match='C_INCLUDE_SG'):validate_design(make_bit(),make_hwh(1))


@pytest.mark.asyncio
async def test_cache_is_bounded_and_fencing_survives_cleanup(tmp_path):
    state=WorkerState('kv2601',tmp_path,FakeBackend());await state.initialize()
    for artifact in ('art_one','art_two','art_three'):
        await deploy(state,artifact=artifact)
        assert [p.name for p in tmp_path.glob('art_*')]==[artifact]
    await state.release('lease_one')
    assert not list(tmp_path.glob('art_*'))
    assert (tmp_path/'fencing.sqlite').is_file()
    with pytest.raises(WorkerConflictError,match='retired'):await deploy(state)
