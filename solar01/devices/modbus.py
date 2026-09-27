"""Modbus RTU framing and a minimal single-slave master."""
from __future__ import annotations

import struct
import time

EXCEPTION_NAMES = {1: 'illegal function', 2: 'illegal data address', 3: 'illegal data value',
                   4: 'slave device failure', 6: 'slave device busy'}


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def with_crc(pdu: bytes) -> bytes:
    c = crc16(pdu)
    return pdu + bytes((c & 0xFF, c >> 8))


def crc_ok(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


class LinkError(Exception):
    """No, short, corrupt or mismatched reply.  The serial port itself is fine."""


class ModbusException(Exception):
    """The slave answered with a Modbus exception."""

    def __init__(self, code: int):
        super().__init__(f'modbus exception {code} ({EXCEPTION_NAMES.get(code, "unknown")})')
        self.code = code


def _default_serial(port: str, baud: int, timeout: float):
    import serial  # imported lazily so pure code and tests do not need pyserial
    return serial.Serial(port, baud, timeout=timeout)


class RtuMaster:
    """Request/response on a half-duplex RS485 line.  Serial-level failures raise
    OSError (the caller should close() and retry later); protocol failures raise
    LinkError or ModbusException."""

    def __init__(self, port: str, baud: int, slave: int = 1, timeout: float = 0.5, gap_s: float = 0.1,
                 serial_factory=None):
        self.port, self.baud, self.slave, self.timeout, self.gap_s = port, baud, slave, timeout, gap_s
        self._factory = serial_factory or _default_serial
        self._ser = None

    def _serial(self):
        if self._ser is None:
            self._ser = self._factory(self.port, self.baud, self.timeout)
        return self._ser

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
        self._ser = None

    def _transact(self, pdu: bytes, expect_len: int) -> bytes:
        ser = self._serial()
        req = with_crc(bytes((self.slave,)) + pdu)
        ser.reset_input_buffer()
        ser.write(req)
        try:
            head = ser.read(5)                    # an exception reply is exactly 5 bytes
            if len(head) < 5:
                raise LinkError(f'no reply ({len(head)} bytes)')
            if head[0] != self.slave:
                raise LinkError(f'reply from slave {head[0]}')
            if head[1] & 0x80:
                if not crc_ok(head):
                    raise LinkError('bad CRC on exception reply')
                raise ModbusException(head[2])
            frame = head + ser.read(expect_len - 5)
            if len(frame) < expect_len:
                raise LinkError(f'short reply ({len(frame)}/{expect_len} bytes)')
            if not crc_ok(frame):
                raise LinkError('bad CRC')
            if frame[1] != pdu[0]:
                raise LinkError(f'function mismatch {frame[1]} != {pdu[0]}')
            return frame
        finally:
            time.sleep(self.gap_s)

    def read_registers(self, function: int, addr: int, count: int) -> tuple[int, ...]:
        frame = self._transact(struct.pack('>BHH', function, addr, count), 5 + 2 * count)
        if frame[2] != 2 * count:
            raise LinkError(f'byte count {frame[2]} != {2 * count}')
        return struct.unpack(f'>{count}H', frame[3:3 + 2 * count])

    def read_input(self, addr: int, count: int) -> tuple[int, ...]:
        return self.read_registers(4, addr, count)

    def read_holding(self, addr: int, count: int) -> tuple[int, ...]:
        return self.read_registers(3, addr, count)

    def write_holding(self, addr: int, value: int) -> None:
        pdu = struct.pack('>BHH', 6, addr, value & 0xFFFF)
        frame = self._transact(pdu, 8)
        if frame[2:6] != pdu[1:5]:
            raise LinkError('write echo mismatch')
