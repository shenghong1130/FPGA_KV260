from types import SimpleNamespace
import pytest
from worker.app.fpga import PynqFpgaBackend,FpgaExecutionError
from worker.tests.test_fpga import make_backend,png_payload

class Mmio:
    def __init__(self,status=0,halts=True):
        self.values={0:1,4:status,0x28:9408};self.halts=halts;self.writes=[]
    def read(self,offset):return self.values.get(offset,0)
    def write(self,offset,value):
        self.writes.append((offset,value));self.values[offset]=value
        if offset==0 and not value&1 and self.halts:self.values[4]=1

def channel(status=0,halts=True,buffer=None):
    return SimpleNamespace(_mmio=Mmio(status,halts),_offset=0,_active_buffer=buffer,_flush_before=True)

@pytest.mark.parametrize('status,match',[(0,'timeout'),(1,'halted'),(0x10,'error'),(0x20,'error'),(0x40,'error')])
def test_dma_wait_is_bounded_and_reports_register_error(status,match):
    backend=PynqFpgaBackend(dma_timeout_seconds=.002)
    with pytest.raises((TimeoutError,RuntimeError),match=match):backend._wait_dma_channel(channel(status),'receive')

def test_dma_completion_preserves_pynq_cache_semantics():
    buffer=SimpleNamespace(invalidated=False)
    buffer.invalidate=lambda:setattr(buffer,'invalidated',True)
    c=channel(2,buffer=buffer);c._flush_before=False
    PynqFpgaBackend()._wait_dma_channel(c,'receive')
    assert buffer.invalidated and c._active_buffer is None and c.transferred==9408

@pytest.mark.asyncio
@pytest.mark.parametrize('halts',[True,False])
async def test_tlast_timeout_stops_both_channels_and_only_frees_proven_safe_buffers(tmp_path,halts):
    backend,dma,events,buffers,bit=make_backend(tmp_path)
    await backend.load_overlay(bit)
    backend.dma_timeout_seconds=.002;backend.dma_stop_timeout_seconds=.002
    mmios=[]
    for name in ('sendchannel','recvchannel'):
        c=getattr(dma,name);c._mmio=Mmio(0,halts);c._offset=0;c._active_buffer=None;c._flush_before=True;mmios.append(c._mmio)
    with pytest.raises(FpgaExecutionError,match='timeout'):
        await backend.predict(png_payload())
    assert all(m.writes==[(0,0)] for m in mmios)
    assert all(b.freed==halts for b in buffers)
    assert (backend.dma is None)==halts
    if not halts:
        assert backend.x_buffer is buffers[0] and backend.y_buffer is buffers[1]
        for m in mmios:m.halts=True
        await backend.release()
        assert all(b.freed for b in buffers)

@pytest.mark.parametrize('value',[0,-1,float('inf'),float('nan')])
def test_invalid_dma_deadline_rejected(value):
    with pytest.raises(ValueError):PynqFpgaBackend(dma_timeout_seconds=value)
