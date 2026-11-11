"""Synthetic structurally valid files for offline tests only; not hardware images."""

def make_bit(marker: bytes = b'offline', part: bytes = b'xck26-sfvc784-2LV-c') -> bytes:
    data = b'\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01'
    for tag, value in ((b'a', marker), (b'b', part), (b'c', b'2026/11/11'), (b'd', b'00:00:00')):
        value += b'\x00'
        data += tag + len(value).to_bytes(2, 'big') + value
    payload = b'\xff' * 8 + b'\xaa\x99\x55\x66' + marker
    return data + b'e' + len(payload).to_bytes(4, 'big') + payload


def make_hwh(sg: int = 0) -> bytes:
    params = {'C_INCLUDE_SG': sg, 'C_INCLUDE_MM2S': 1, 'C_INCLUDE_S2MM': 1,
              'C_M_AXI_MM2S_ADDR_WIDTH': 32, 'C_M_AXI_S2MM_ADDR_WIDTH': 32,
              'C_M_AXI_MM2S_DATA_WIDTH': 32, 'C_M_AXI_S2MM_DATA_WIDTH': 32,
              'C_M_AXIS_MM2S_TDATA_WIDTH': 32, 'C_S_AXIS_S2MM_TDATA_WIDTH': 32,
              'C_SG_LENGTH_WIDTH': 14}
    xml = '<EDKSYSTEM><SYSTEMINFO DEVICE="xck26" PACKAGE="sfvc784"/><MODULES><MODULE INSTANCE="axi_dma_0" MODTYPE="axi_dma"><PARAMETERS>'
    xml += ''.join(f'<PARAMETER NAME="{n}" VALUE="{v}"/>' for n, v in params.items())
    xml += '</PARAMETERS><BUSINTERFACES>'
    xml += ''.join(f'<BUSINTERFACE NAME="{n}" BUSNAME="net_{n}"/>' for n in ('S_AXI_LITE', 'M_AXI_MM2S', 'M_AXI_S2MM', 'M_AXIS_MM2S', 'S_AXIS_S2MM'))
    return (xml + '</BUSINTERFACES></MODULE></MODULES></EDKSYSTEM>').encode()
