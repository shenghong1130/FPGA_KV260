"""Static checks for the KV260 PYNQ ABI. Keep identical in server and worker.

These checks cannot prove that a bitstream implements the accompanying HWH or
that its data path terminates; physical verification is still required.
"""
from __future__ import annotations

import re
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path


class HardwareValidationError(ValueError):
    pass


def validate_design(bit: bytes, hwh: bytes) -> None:
    if b'<!DOCTYPE' in hwh.upper() or b'<!ENTITY' in hwh.upper():
        raise HardwareValidationError("HWH DTD/entities are unsupported")
    try:
        root = ET.fromstring(hwh)
    except ET.ParseError as exc:
        raise HardwareValidationError(f"invalid HWH XML: {exc}") from exc
    modules = [m for m in root.iter('MODULE') if m.get('INSTANCE') == 'axi_dma_0']
    if len(modules) != 1 or modules[0].get('MODTYPE') != 'axi_dma':
        raise HardwareValidationError("HWH requires exactly one axi_dma_0 (axi_dma)")
    dma = modules[0]
    params = {p.get('NAME'): p.get('VALUE') for p in dma.iter('PARAMETER')}

    def number(name: str) -> int:
        try:
            value = params[name]
            return int(value, 16 if value.lower().startswith('0x') else 10)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise HardwareValidationError(f"axi_dma_0 missing/invalid {name}") from exc

    if number('C_INCLUDE_SG') != 0:
        raise HardwareValidationError("axi_dma_0 must use Simple DMA: C_INCLUDE_SG=0; Scatter Gather is unsupported")
    for channel in ('MM2S', 'S2MM'):
        if number(f'C_INCLUDE_{channel}') != 1:
            raise HardwareValidationError(f"axi_dma_0 requires {channel} channel")
        if number(f'C_M_AXI_{channel}_ADDR_WIDTH') not in (32, 64):
            raise HardwareValidationError(f"unsupported {channel} address width")
        if number(f'C_M_AXI_{channel}_DATA_WIDTH') not in (32, 64, 128):
            raise HardwareValidationError(f"unsupported {channel} memory data width")
    for name in ('C_M_AXIS_MM2S_TDATA_WIDTH', 'C_S_AXIS_S2MM_TDATA_WIDTH'):
        if number(name) not in (32, 64, 128):
            raise HardwareValidationError(f"unsupported stream width: {name}")
    if not 14 <= number('C_SG_LENGTH_WIDTH') <= 26:
        raise HardwareValidationError("DMA length width cannot transfer the 9408-byte input")
    interfaces = {b.get('NAME'): b for b in dma.iter('BUSINTERFACE')}
    for name in ('S_AXI_LITE', 'M_AXI_MM2S', 'M_AXI_S2MM', 'M_AXIS_MM2S', 'S_AXIS_S2MM'):
        if name not in interfaces or not interfaces[name].get('BUSNAME'):
            raise HardwareValidationError(f"axi_dma_0 missing/unconnected interface {name}")
    infos = list(root.iter('SYSTEMINFO'))
    if len(infos) != 1 or infos[0].get('DEVICE', '').lower() != 'xck26' or infos[0].get('PACKAGE', '').lower() != 'sfvc784':
        raise HardwareValidationError("HWH target must be KV260 xck26/sfvc784")
    try:
        # Xilinx .bit header: preamble, 0x0001, a/b/c/d strings, e payload.
        if bit[:13] != b'\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01':
            raise ValueError('invalid BIT preamble')
        offset = 13
        fields = {}
        for tag in b'abcd':
            if bit[offset] != tag:
                raise ValueError('invalid BIT header fields')
            size = int.from_bytes(bit[offset+1:offset+3], 'big')
            offset += 3
            value = bit[offset:offset+size]
            if not size or len(value) != size or value[-1] != 0:
                raise ValueError('truncated BIT header')
            fields[chr(tag)] = value[:-1].decode('ascii')
            offset += size
        if not re.fullmatch(r'xck26-sfvc784-2[lL][vV]-[cCiIeE]', fields['b']):
            raise ValueError(f"BIT target must be xck26-sfvc784-2LV-c: {fields['b']}")
        if bit[offset:offset+1] != b'e':
            raise ValueError('missing BIT payload')
        size = int.from_bytes(bit[offset+1:offset+5], 'big')
        payload = bit[offset+5:]
        if size != len(payload) or size < 4 or b'\xaa\x99\x55\x66' not in payload:
            raise ValueError('invalid BIT payload size/sync word')
    except (ValueError, IndexError, UnicodeError) as exc:
        raise HardwareValidationError(str(exc)) from exc


def validate_paths(bit: Path, hwh: Path,
                   bit_sha256: str | None = None, hwh_sha256: str | None = None) -> None:
    bit_data, hwh_data = bit.read_bytes(), hwh.read_bytes()
    for data, expected in ((bit_data, bit_sha256), (hwh_data, hwh_sha256)):
        if expected is not None and hashlib.sha256(data).hexdigest() != expected:
            raise HardwareValidationError("artifact SHA-256 mismatch")
    validate_design(bit_data, hwh_data)
