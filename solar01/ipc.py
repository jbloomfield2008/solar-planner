"""Local IPC between core and hub: newline-delimited JSON over a unix socket.

``tcp://host:port`` addresses are accepted for development and tests (Windows has no
unix sockets).  The core side is thread based and never blocks on a slow client:
each client has a bounded queue and is dropped when it cannot keep up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import socket
import threading
import time

log = logging.getLogger('solar01.ipc')
MAX_LINE = 1 << 20


def parse_address(address: str):
    if address.startswith('tcp://'):
        host, port = address[6:].rsplit(':', 1)
        return 'tcp', (host, int(port))
    return 'unix', address


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(',', ':'), default=str) + '\n').encode()


class ServerClient:
    def __init__(self, server: 'IpcServer', sock: socket.socket):
        self.server = server
        self.sock = sock
        self.name = '?'
        self.alive = True
        self._q: queue.Queue = queue.Queue(maxsize=256)
        threading.Thread(target=self._writer, name='ipc-writer', daemon=True).start()
        threading.Thread(target=self._reader, name='ipc-reader', daemon=True).start()

    def send(self, data: bytes) -> None:
        if not self.alive:
            return
        try:
            self._q.put_nowait(data)
        except queue.Full:
            log.warning('ipc client %s is not reading, dropping it', self.name)
            self.close()

    def send_msg(self, msg: dict) -> None:
        self.send(encode(msg))

    def _writer(self) -> None:
        while True:
            data = self._q.get()
            if data is None or not self.alive:
                return
            try:
                self.sock.sendall(data)
            except OSError:
                self.close()
                return

    def _reader(self) -> None:
        buf = b''
        try:
            while self.alive:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_LINE and b'\n' not in buf:
                    log.warning('ipc client %s sent an oversized line', self.name)
                    break
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        log.warning('ipc client %s sent invalid JSON', self.name)
                        continue
                    if isinstance(msg, dict):
                        self.server.dispatch(self, msg)
        except OSError:
            pass
        finally:
            self.close()

    def close(self) -> None:
        if not self.alive:
            return
        self.alive = False
        for fn in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                fn()
            except OSError:
                pass
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self.server.remove(self)


class IpcServer:
    def __init__(self, address: str, on_message, on_connect=None):
        self.address = address
        self.on_message = on_message
        self.on_connect = on_connect
        self.clients: set[ServerClient] = set()
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.closed = False

    def start(self) -> None:
        kind, addr = parse_address(self.address)
        if kind == 'unix':
            try:
                os.unlink(addr)
            except FileNotFoundError:
                pass
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(addr)
            os.chmod(addr, 0o660)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(addr)
        sock.listen(8)
        self.sock = sock
        threading.Thread(target=self._accept, name='ipc-accept', daemon=True).start()

    def bound_address(self) -> str:
        kind, addr = parse_address(self.address)
        if kind == 'tcp' and self.sock is not None:
            return f'tcp://{addr[0]}:{self.sock.getsockname()[1]}'
        return self.address

    def _accept(self) -> None:
        while not self.closed:
            try:
                s, _ = self.sock.accept()
            except OSError:
                if self.closed:
                    return
                time.sleep(0.5)
                continue
            client = ServerClient(self, s)
            with self.lock:
                self.clients.add(client)
            if self.on_connect:
                try:
                    self.on_connect(client)
                except Exception:  # noqa: BLE001
                    log.exception('ipc on_connect')

    def dispatch(self, client: ServerClient, msg: dict) -> None:
        try:
            self.on_message(client, msg)
        except Exception:  # noqa: BLE001
            log.exception('ipc message handler')

    def remove(self, client: ServerClient) -> None:
        with self.lock:
            self.clients.discard(client)

    def broadcast(self, msg: dict) -> None:
        data = encode(msg)
        with self.lock:
            clients = list(self.clients)
        for c in clients:
            c.send(data)

    def client_names(self) -> list[str]:
        with self.lock:
            return sorted(c.name for c in self.clients)

    def close(self) -> None:
        self.closed = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        with self.lock:
            clients = list(self.clients)
        for c in clients:
            c.close()


class CoreClient:
    """Hub side: a self-healing connection to core with request/response for writes."""

    def __init__(self, address: str, on_message, name: str = 'hub'):
        self.address = address
        self.on_message = on_message
        self.name = name
        self.connected = False
        self.last_message_mono: float | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._last_log = 0.0

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                kind, addr = parse_address(self.address)
                if kind == 'unix':
                    reader, writer = await asyncio.open_unix_connection(addr, limit=MAX_LINE)
                else:
                    reader, writer = await asyncio.open_connection(addr[0], addr[1], limit=MAX_LINE)
                self._writer = writer
                self.connected = True
                backoff = 1.0
                log.info('connected to core at %s', self.address)
                await self.send({'type': 'hello', 'name': self.name})
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    msg = json.loads(line)
                    self.last_message_mono = time.monotonic()
                    fut = self._pending.pop(msg.get('id'), None) if msg.get('type') == 'result' else None
                    if fut is not None:
                        if not fut.done():
                            fut.set_result(msg)
                    else:
                        self.on_message(msg)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, asyncio.IncompleteReadError) as e:
                if time.monotonic() - self._last_log > 60:
                    log.warning('core link: %s', e)
                    self._last_log = time.monotonic()
            except Exception:  # noqa: BLE001
                log.exception('core link')
            finally:
                self.connected = False
                if self._writer is not None:
                    self._writer.close()
                self._writer = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError('core connection lost'))
                self._pending.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def send(self, msg: dict) -> None:
        if self._writer is None:
            raise ConnectionError('not connected to core')
        self._writer.write(encode(msg))
        await self._writer.drain()

    async def request(self, msg: dict, timeout: float = 30.0) -> dict:
        self._seq += 1
        rid = f'{os.getpid()}-{self._seq}'
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self.send(dict(msg, id=rid))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)
