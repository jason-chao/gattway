"""asyncio client for a gattway instance.

    async with Gattway("ws://127.0.0.1:7120/ws", name="my-app") as g:
        devices = await g.scan(timeout_s=3)
        dev = await g.connect_device(devices[0]["address"])
        await dev.subscribe("fa02", lambda data, t: print(data.hex()))
        await dev.write("fa01", b"\\x01\\x02")

The client never reconnects on its own. When the socket closes, pending
requests fail with ``GattwayError("closed")``, ``closed`` is set and
``on_closed`` fires; the application decides what to do next.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable, Iterable

from websockets.exceptions import ConnectionClosed

from . import PROTOCOL, PROTOCOL_VERSION
from .errors import CLOSED, GattwayError
from .uuids import expand_uuid

try:  # websockets >= 13
    from websockets.asyncio.client import connect as _ws_connect
except ImportError:  # pragma: no cover - websockets 12
    from websockets.client import connect as _ws_connect  # type: ignore[no-redef]

log = logging.getLogger("gattway.client")

Callback = Callable[..., Any]
NotifyCallback = Callable[[bytes, float], Any]

HEARTBEAT_INTERVAL_S = 2.0
DEFAULT_TIMEOUT_S = 10.0
OFFSET_SAMPLES = 8


def _call(cb: Callback | None, *args: Any) -> None:
    if cb is None:
        return
    try:
        result = cb(*args)
        if inspect.isawaitable(result):
            asyncio.get_running_loop().create_task(result)  # type: ignore[arg-type]
    except Exception:
        log.exception("callback failed")


def _hex(data: bytes | bytearray | memoryview | str) -> str:
    if isinstance(data, str):
        return bytes.fromhex(data.replace(" ", "")).hex()
    return bytes(data).hex()


class Gattway:
    """A connection to one gattway instance."""

    def __init__(
        self,
        url: str,
        name: str,
        token: str | None = None,
        *,
        heartbeat: bool = True,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.url = url
        self.name = name
        self.token = token
        self.timeout_s = timeout_s
        self._heartbeat = heartbeat
        self._hb_interval = heartbeat_interval_s

        self.hello: dict[str, Any] | None = None
        self.status: dict[str, Any] | None = None
        self.closed = asyncio.Event()
        self.close_code: int | None = None
        self.close_reason: str | None = None

        self.on_status: Callback | None = None
        self.on_notify: Callback | None = None  # (address, char, data: bytes, t)
        self.on_disconnected: Callback | None = None  # (message dict)
        self.on_closed: Callback | None = None  # ()

        self._ws: Any = None
        self._seq = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._devices: dict[str, Device] = {}
        self._hello_event = asyncio.Event()
        self._first_error: GattwayError | None = None
        self._samples: deque[tuple[float, float]] = deque(maxlen=OFFSET_SAMPLES)  # (rtt, offset)
        self._offset: float | None = None
        self._rtt: float | None = None

    # -- connection ---------------------------------------------------------

    @classmethod
    async def open(cls, url: str, name: str, token: str | None = None, **kwargs: Any) -> "Gattway":
        g = cls(url, name, token, **kwargs)
        await g.connect()
        return g

    async def connect(self) -> dict[str, Any]:
        """Open the socket, wait for the instance's hello, and send ours."""
        url = self.url
        if self.token:
            url += ("&" if "?" in url else "?") + "token=" + self.token
        self._ws = await _ws_connect(url, ping_interval=None, max_size=1 << 20)
        self._tasks.append(asyncio.get_running_loop().create_task(self._reader()))
        try:
            await asyncio.wait_for(self._hello_event.wait(), self.timeout_s)
        except asyncio.TimeoutError:
            await self.close()
            raise GattwayError("timeout", "no hello from instance") from None
        if self._first_error is not None:
            await self.close()
            raise self._first_error
        if self.hello is None:
            raise GattwayError(CLOSED, "connection closed before hello")
        if self.hello.get("protocol") != PROTOCOL or int(self.hello.get("version", 0)) != PROTOCOL_VERSION:
            await self.close()
            raise GattwayError("bad_request", f"instance speaks {self.hello.get('protocol')} v{self.hello.get('version')}")
        if self._heartbeat:
            self._tasks.append(asyncio.get_running_loop().create_task(self._heartbeat_loop()))
        else:
            await self.heartbeat()
        return await self.request("hello", name=self.name, protocol=PROTOCOL, version=PROTOCOL_VERSION)

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._finish(None, None)
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()

    async def __aenter__(self) -> "Gattway":
        if self._ws is None:
            await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self.closed.is_set()

    # -- time ---------------------------------------------------------------

    @property
    def offset(self) -> float:
        """instance_time - local_time, estimated from heartbeat echoes."""
        return self._offset if self._offset is not None else 0.0

    @property
    def rtt(self) -> float | None:
        return self._rtt

    def instance_time(self, local_t: float | None = None) -> float:
        """Convert a local ``time.time()`` value (default now) to the instance's clock."""
        return (time.time() if local_t is None else local_t) + self.offset

    async def heartbeat(self) -> None:
        if self._ws is not None and not self.closed.is_set():
            await self._ws.send(json.dumps({"type": "hb", "t": time.time()}))

    async def _heartbeat_loop(self) -> None:
        try:
            while not self.closed.is_set():
                await self.heartbeat()
                await asyncio.sleep(self._hb_interval)
        except (ConnectionClosed, asyncio.CancelledError):
            pass

    def _on_hb(self, msg: dict[str, Any]) -> None:
        echo = msg.get("echo")
        t_inst = msg.get("t")
        if not isinstance(echo, (int, float)) or not isinstance(t_inst, (int, float)):
            return
        now = time.time()
        rtt = max(0.0, now - echo)
        offset = t_inst - (echo + rtt / 2)
        self._samples.append((rtt, offset))
        best = min(self._samples)
        self._rtt, self._offset = best

    # -- requests -----------------------------------------------------------

    async def request(self, op: str, *, timeout: float | None = None, **args: Any) -> Any:
        """Send one request and return its ``result``; raise GattwayError on ``ok: false``."""
        if self._ws is None or self.closed.is_set():
            raise GattwayError(CLOSED, "not connected")
        self._seq += 1
        req_id = str(self._seq)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        msg = {"id": req_id, "op": op, **{k: v for k, v in args.items() if v is not None}}
        try:
            await self._ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout if timeout is not None else self.timeout_s)
        except asyncio.TimeoutError:
            raise GattwayError("timeout", f"no reply to {op} within timeout") from None
        except ConnectionClosed:
            raise GattwayError(CLOSED, "connection closed") from None
        finally:
            self._pending.pop(req_id, None)

    async def radios(self) -> list[dict[str, Any]]:
        return (await self.request("radios"))["radios"]

    async def scan(
        self,
        timeout_s: float = 5.0,
        name_prefix: str | None = None,
        services: Iterable[str] | None = None,
        radio: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self.request(
            "scan",
            timeout=timeout_s + self.timeout_s,
            radio=radio,
            timeout_s=timeout_s,
            name_prefix=name_prefix,
            services=list(services) if services is not None else None,
        )
        return result["devices"]

    async def connect_device(self, address: str, radio: str | None = None, timeout_s: float = 20.0) -> "Device":
        """Connect to a device and become its owner."""
        address = address.lower()
        result = await self.request(
            "connect", timeout=timeout_s + self.timeout_s, address=address, radio=radio, timeout_s=timeout_s
        )
        dev = self._devices.get(address)
        if dev is None or not dev.connected:
            dev = Device(self, result)
            self._devices[address] = dev
        else:
            dev._info = result
        return dev

    def device(self, address: str) -> "Device | None":
        return self._devices.get(address.lower())

    # -- incoming -----------------------------------------------------------

    async def _reader(self) -> None:
        code = reason = None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("bad frame from instance: %r", raw[:80])
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except ConnectionClosed as exc:
            code, reason = exc.rcvd.code if exc.rcvd else None, exc.rcvd.reason if exc.rcvd else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            log.exception("reader failed: %s", exc)
        finally:
            self._finish(code, reason)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "reply":
            req_id = msg.get("id")
            fut = self._pending.get(str(req_id)) if req_id is not None else None
            if fut is not None and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result", {}))
                else:
                    err = msg.get("error") or {}
                    fut.set_exception(GattwayError(err.get("code", "error"), err.get("message", ""), err.get("data")))
            elif req_id is None and not msg.get("ok") and not self._hello_event.is_set():
                err = msg.get("error") or {}
                self._first_error = GattwayError(err.get("code", "error"), err.get("message", ""), err.get("data"))
                self._hello_event.set()
        elif kind == "hello":
            self.hello = msg
            t = msg.get("t")
            if isinstance(t, (int, float)) and self._offset is None:
                self._offset = t - time.time()
            self._hello_event.set()
        elif kind == "status":
            self.status = msg
            _call(self.on_status, msg)
        elif kind == "hb":
            self._on_hb(msg)
        elif kind == "notify":
            address = str(msg.get("address", "")).lower()
            char = msg.get("char")
            try:
                data = bytes.fromhex(str(msg.get("data", "")).replace(" ", ""))
            except ValueError:
                return
            t = msg.get("t", time.time())
            dev = self._devices.get(address)
            if dev is not None:
                dev._notify(char, data, t)
            _call(self.on_notify, address, char, data, t)
        elif kind == "disconnected":
            address = str(msg.get("address", "")).lower()
            dev = self._devices.pop(address, None)
            if dev is not None:
                dev._ended(msg)
            _call(self.on_disconnected, msg)

    def _finish(self, code: int | None, reason: str | None) -> None:
        if self.closed.is_set():
            return
        self.close_code, self.close_reason = code, reason
        self.closed.set()
        self._hello_event.set()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(GattwayError(CLOSED, f"connection closed ({code})"))
        self._pending.clear()
        for dev in list(self._devices.values()):
            dev._ended({"type": "disconnected", "address": dev.address, "reason": "closed", "requested": False, "t": time.time()})
        self._devices.clear()
        _call(self.on_closed)


class Device:
    """A device this client owns on the instance."""

    def __init__(self, gattway: Gattway, info: dict[str, Any]):
        self._g = gattway
        self._info = info
        self.address: str = info["address"]
        self.connected = True
        self.disconnected = asyncio.Event()
        self.disconnect_reason: str | None = None
        self.on_disconnected: Callback | None = None  # (message dict)
        self._subs: dict[str, NotifyCallback] = {}

    def __repr__(self) -> str:
        return f"Device({self.address}, name={self.name!r}, connected={self.connected})"

    @property
    def name(self) -> str | None:
        return self._info.get("name")

    @property
    def radio(self) -> str | None:
        return self._info.get("radio")

    @property
    def services(self) -> list[dict[str, Any]]:
        return self._info.get("services", [])

    def characteristics(self) -> list[dict[str, Any]]:
        return [c for s in self.services for c in s.get("characteristics", [])]

    def instance_time(self, local_t: float | None = None) -> float:
        return self._g.instance_time(local_t)

    async def _req(self, op: str, **args: Any) -> Any:
        return await self._g.request(op, address=self.address, **args)

    async def write(self, char: str, data: bytes | str, response: bool = False) -> None:
        await self._req("write", char=expand_uuid(char), data=_hex(data), response=response)

    async def write_at(
        self, char: str, frames: Iterable[tuple[float, bytes | str]], response: bool = False
    ) -> dict[str, int]:
        """Queue ``(t, data)`` frames; ``t`` in instance time (see ``instance_time``)."""
        return await self._req(
            "write_at",
            char=expand_uuid(char),
            response=response,
            frames=[{"t": float(t), "data": _hex(d)} for t, d in frames],
        )

    async def cancel(self, char: str | None = None) -> int:
        result = await self._req("cancel", char=expand_uuid(char) if char else None)
        return result["cancelled"]

    async def read(self, char: str) -> bytes:
        result = await self._req("read", char=expand_uuid(char))
        return bytes.fromhex(result["data"])

    async def subscribe(self, char: str, callback: NotifyCallback | None = None) -> None:
        """Subscribe; ``callback(data: bytes, t: float)`` runs for each notification."""
        uuid = expand_uuid(char)
        if callback is not None:
            self._subs[uuid] = callback
        await self._req("subscribe", char=uuid)

    async def unsubscribe(self, char: str) -> None:
        uuid = expand_uuid(char)
        await self._req("unsubscribe", char=uuid)
        self._subs.pop(uuid, None)

    async def farewell(self, writes: Iterable[tuple[str, bytes | str] | dict[str, Any]]) -> None:
        """Register the writes the instance performs if this client is lost.

        Each item is ``(char, data)`` or ``{"char", "data", "response"?}``.
        """
        out = []
        for w in writes:
            if isinstance(w, dict):
                out.append({"char": expand_uuid(w["char"]), "data": _hex(w["data"]), "response": bool(w.get("response", False))})
            else:
                char, data = w
                out.append({"char": expand_uuid(char), "data": _hex(data), "response": False})
        await self._req("farewell", writes=out)

    async def disconnect(self) -> None:
        """Disconnect without running the farewell writes."""
        await self._req("disconnect")
        self._g._devices.pop(self.address, None)
        self._ended({"type": "disconnected", "address": self.address, "reason": "requested", "requested": True, "t": time.time()})

    def _notify(self, char: str | None, data: bytes, t: float) -> None:
        if char is None:
            return
        cb = self._subs.get(char)
        if cb is not None:
            _call(cb, data, t)

    def _ended(self, msg: dict[str, Any]) -> None:
        if not self.connected:
            return
        self.connected = False
        self.disconnect_reason = msg.get("reason")
        self.disconnected.set()
        _call(self.on_disconnected, msg)
