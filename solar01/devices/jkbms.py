"""JK BMS (16S 280 Ah, fw 11.XA) over its classic "NW" RS485 protocol at 115200 8N1."""
from __future__ import annotations

import struct
import time

REQUEST = bytes.fromhex('4e5700130000000006030000000000006800000129')

# field id -> payload length (0x79 cell block is length-prefixed)
FIELD_LEN = {0x80: 2, 0x81: 2, 0x82: 2, 0x83: 2, 0x84: 2, 0x85: 1, 0x86: 1, 0x87: 2, 0x89: 4, 0x8a: 2, 0x8b: 2,
             0x8c: 2, 0x8e: 2, 0x8f: 2, 0x90: 2, 0x91: 2, 0x92: 2, 0x93: 2, 0x94: 2, 0x95: 2, 0x96: 2, 0x97: 2,
             0x98: 2, 0x99: 2, 0x9a: 2, 0x9b: 2, 0x9c: 2, 0x9d: 1, 0x9e: 2, 0x9f: 2, 0xa0: 2, 0xa1: 2, 0xa2: 2,
             0xa3: 2, 0xa4: 2, 0xa5: 2, 0xa6: 2, 0xa7: 2, 0xa8: 2, 0xa9: 1, 0xaa: 4, 0xab: 1, 0xac: 1, 0xad: 2,
             0xae: 1, 0xaf: 1, 0xb0: 2, 0xb1: 1, 0xb2: 10, 0xb3: 1, 0xb4: 8, 0xb5: 4, 0xb6: 4, 0xb7: 15, 0xb8: 1,
             0xb9: 4, 0xba: 24, 0xbb: 1, 0xbc: 1, 0xbd: 1, 0xbe: 1, 0xbf: 1, 0xc0: 1}
NEEDED = (0x79, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x87, 0x8b, 0x8c, 0xaa)


def _u16(b: bytes, i: int = 0) -> int:
    return (b[i] << 8) | b[i + 1]


def _temp(raw: int) -> int:
    """JK temperatures: 0..100 = degrees C, 101..140 = -1..-40."""
    return raw if raw <= 100 else 100 - raw


def parse(raw: bytes, cells_expected: int | None = None) -> dict | None:
    """Decode a response frame.  Returns None for anything incomplete or implausible,
    because the BMS emulator forwards these values to the inverter."""
    if len(raw) < 200 or raw[:2] != b'NW':
        return None
    d = raw[11:]
    fields: dict[int, bytes] = {}
    i = 0
    while i < len(d) - 5:
        fid = d[i]
        if fid == 0x68:
            break
        if fid == 0x79:
            n = d[i + 1]
            fields[fid] = d[i + 2:i + 2 + n]
            i += 2 + n
        elif fid in FIELD_LEN:
            n = FIELD_LEN[fid]
            fields[fid] = d[i + 1:i + 1 + n]
            i += 1 + n
        else:
            break
    if any(k not in fields for k in NEEDED):
        return None
    cells: dict[int, int] = {}
    cv = fields[0x79]
    for j in range(0, len(cv) - 2, 3):
        cells[cv[j]] = _u16(cv, j + 1)
    vals = list(cells.values())
    raw_cur = _u16(fields[0x84])
    current = (raw_cur & 0x7FFF) / 100
    if not raw_cur & 0x8000:
        current = -current                       # discharge negative, charge positive
    voltage = _u16(fields[0x83]) / 100
    soc = fields[0x85][0]
    capacity = struct.unpack('>I', fields[0xaa])[0]
    status = _u16(fields[0x8c])
    if not vals or (cells_expected and len(vals) != cells_expected):
        return None
    if not all(1500 <= v <= 4500 for v in vals) or not 20 <= voltage <= 70 or soc > 100:
        return None
    return {
        'voltage': voltage,
        'current': current,
        'power': round(voltage * current, 1),
        'soc': soc,
        'capacity': capacity,
        'remaining_capacity': round(capacity * soc / 100, 1),
        'cycle_count': _u16(fields[0x87]),
        'temp_mos': _temp(_u16(fields[0x80])),
        'temp_1': _temp(_u16(fields[0x81])),
        'temp_2': _temp(_u16(fields[0x82])),
        'cell_voltage_min': min(vals),
        'cell_voltage_max': max(vals),
        'cell_voltage_delta': max(vals) - min(vals),
        'cells': {f'cell_{k:02d}': v for k, v in sorted(cells.items())},
        'charge_mos_on': bool(status & 1),
        'discharge_mos_on': bool(status & 2),
        'balancing': bool(status & 4),
        'warning_bits': _u16(fields[0x8b]),
    }


def _default_serial(port: str, baud: int, timeout: float):
    import serial
    return serial.Serial(port, baud, timeout=timeout)


class JkBms:
    def __init__(self, port: str, baud: int = 115200, cells: int | None = 16, settle_s: float = 0.7,
                 serial_factory=None):
        self.port, self.baud, self.cells, self.settle_s = port, baud, cells, settle_s
        self._factory = serial_factory or _default_serial
        self._ser = None

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
        self._ser = None

    def poll(self) -> dict | None:
        """One request/response.  OSError means the port failed (close and retry later);
        None means the reply was missing or not plausible."""
        if self._ser is None:
            self._ser = self._factory(self.port, self.baud, 0.5)
        self._ser.reset_input_buffer()
        self._ser.write(REQUEST)
        time.sleep(self.settle_s)
        return parse(self._ser.read(4096), self.cells)
