"""The gattway WebSocket server. Implements docs/PROTOCOL.md version 1."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import signal
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from websockets.exceptions import ConnectionClosed

from . import PROTOCOL, PROTOCOL_VERSION, __version__
from .config import Config, normalise_address
from .errors import (
    BAD_REQUEST,
    BUSY,
    HELD,
    NOT_CONNECTED,
    NOT_OWNER,
    NO_RADIO,
    UNAUTHORISED,
    UNKNOWN_OP,
    GattwayError,
)
from .radios.base import DeviceLink, Radio, RadioBackend, RadioFactory
from .uuids import expand_uuid

try:  # websockets >= 13
    from websockets.asyncio.server import serve as _ws_serve

    def _request_path(ws: Any) -> str:
        return ws.request.path

except ImportError:  # pragma: no cover - websockets 12
    from websockets.server import serve as _ws_serve  # type: ignore[no-redef]

    def _request_path(ws: Any) -> str:
        return ws.path


log = logging.getLogger("gattway.server")

CLOSE_UNAUTHORISED = 4001
CLOSE_HEARTBEAT_LOST = 4000
CLOSE_GOING_AWAY = 1001

SCAN_MIN_S, SCAN_MAX_S, SCAN_DEFAULT_S = 1.0, 30.0, 5.0
CONNECT_DEFAULT_S = 20.0

OpHandler = Callable[["ClientState", dict[str, Any]], Awaitable[Any]]


# ----------------------------------------------------------------------------
# State


@dataclass(eq=False)
class ClientState:
    ws: Any
    name: str
    connected_at: float
    last_hb: float
    outbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    writer: asyncio.Task | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)
    gone: bool = False

    def send(self, msg: dict[str, Any]) -> None:
        if not self.gone:
            self.outbox.put_nowait(json.dumps(msg, separators=(",", ":")))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "connected_at": self.connected_at}


@dataclass(eq=False)
class RadioState:
    address: str
    label: str | None
    enabled: bool
    default: bool
    configured: bool
    radio: Radio | None = None
    hci: str | None = None
    bus: str | None = None
    powered: bool | None = None
    blocked: bool | None = None
    scanning: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def present(self) -> bool:
        return self.radio is not None

    @property
    def name(self) -> str:
        return self.label or self.address

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "address": self.address,
            "hci": self.hci,
            "bus": self.bus,
            "enabled": self.enabled,
            "default": self.default,
            "present": self.present,
            "powered": self.powered,
            "blocked": self.blocked,
            "scanning": self.scanning,
        }


class TimedQueue:
    """Frames to write to one device at absolute instance times (section 6)."""

    def __init__(self, link: DeviceLink, late_s: float):
        self.link = link
        self.late_s = late_s
        self.entries: dict[tuple[str, float], tuple[bytes, bool]] = {}
        self.written = 0
        self.late = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def add(self, char: str, frames: list[tuple[float, bytes]], response: bool) -> dict[str, int]:
        now = time.time()
        queued = replaced = late = 0
        for t, data in frames:
            if now - t > self.late_s:
                late += 1
                self.late += 1
                continue
            key = (char, t)
            if key in self.entries:
                replaced += 1
            self.entries[key] = (data, response)
            queued += 1
        self._ensure_task()
        self._wake.set()
        return {"queued": queued, "replaced": replaced, "late": late}

    def cancel(self, char: str | None = None) -> int:
        if char is None:
            n = len(self.entries)
            self.entries.clear()
        else:
            keys = [k for k in self.entries if k[0] == char]
            for k in keys:
                del self.entries[k]
            n = len(keys)
        self._wake.set()
        return n

    def stop(self) -> None:
        self.entries.clear()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def to_json(self) -> dict[str, int]:
        return {"pending": len(self.entries), "written": self.written, "late": self.late}

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            if not self.entries:
                await self._wake.wait()
                self._wake.clear()
                continue
            key = min(self.entries, key=lambda k: k[1])
            delay = key[1] - time.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                else:
                    self._wake.clear()
                    continue  # queue changed; pick the earliest again
            entry = self.entries.pop(key, None)
            if entry is None:
                continue
            if time.time() - key[1] > self.late_s:
                self.late += 1
                continue
            data, response = entry
            try:
                await self.link.write(key[0], data, response)
                self.written += 1
            except Exception as exc:
                log.warning("timed write to %s %s failed: %s", self.link.address, key[0], exc)


@dataclass(eq=False)
class DeviceState:
    address: str
    name: str | None
    radio: RadioState
    link: DeviceLink
    owner: ClientState
    connected_at: float
    queue: TimedQueue
    subscribed: set[str] = field(default_factory=set)
    farewell: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "radio": self.radio.name,
            "owner": self.owner.name,
            "connected_at": self.connected_at,
            "subscribed": sorted(self.subscribed),
            "farewell": len(self.farewell),
            "queue": self.queue.to_json(),
        }


# ----------------------------------------------------------------------------
# Argument helpers


def _str_arg(msg: dict[str, Any], key: str, required: bool = True) -> str | None:
    v = msg.get(key)
    if v is None:
        if required:
            raise GattwayError(BAD_REQUEST, f"missing {key}")
        return None
    if not isinstance(v, str):
        raise GattwayError(BAD_REQUEST, f"{key} must be a string")
    return v


def _address_arg(msg: dict[str, Any]) -> str:
    return normalise_address(_str_arg(msg, "address"))  # type: ignore[arg-type]


def _char_arg(msg: dict[str, Any], key: str = "char") -> str:
    return expand_uuid(_str_arg(msg, key))  # type: ignore[arg-type]


def _hex_arg(value: Any, key: str = "data") -> bytes:
    if not isinstance(value, str):
        raise GattwayError(BAD_REQUEST, f"{key} must be a hex string")
    try:
        return bytes.fromhex(value.replace(" ", ""))
    except ValueError:
        raise GattwayError(BAD_REQUEST, f"{key} is not hex") from None


def _bool_arg(msg: dict[str, Any], key: str, default: bool = False) -> bool:
    v = msg.get(key, default)
    if not isinstance(v, bool):
        raise GattwayError(BAD_REQUEST, f"{key} must be a boolean")
    return v


def _number_arg(msg: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    v = msg.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise GattwayError(BAD_REQUEST, f"{key} must be a number")
    if not lo <= v <= hi:
        raise GattwayError(BAD_REQUEST, f"{key} must be between {lo:g} and {hi:g}")
    return float(v)


# ----------------------------------------------------------------------------
# Server


class GattwayServer:
    """One instance: a WebSocket endpoint lending the radios of ``backend``."""

    def __init__(self, config: Config, backend: RadioBackend):
        self.config = config
        self.backend = backend
        self.clients: list[ClientState] = []
        self.devices: dict[str, DeviceState] = {}
        self.radios: dict[str, RadioState] = {}
        self._server: Any = None
        self._tasks: set[asyncio.Task] = set()
        self._status_pending = False
        self._client_seq = 0
        self._stopping = False
        self._ops: dict[str, OpHandler] = {
            "hello": self._op_hello,
            "radios": self._op_radios,
            "scan": self._op_scan,
            "connect": self._op_connect,
            "disconnect": self._op_disconnect,
            "farewell": self._op_farewell,
            "write": self._op_write,
            "write_at": self._op_write_at,
            "cancel": self._op_cancel,
            "read": self._op_read,
            "subscribe": self._op_subscribe,
            "unsubscribe": self._op_unsubscribe,
        }

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.refresh_radios()
        self._server = await _ws_serve(
            self._handler,
            self.config.server.host,
            self.config.server.port,
            ping_interval=None,  # the protocol has its own heartbeat
            max_size=1 << 20,
        )
        self._spawn(self._periodic())
        log.info("gattway %s listening on %s:%d", __version__, self.config.server.host, self.port)

    @property
    def port(self) -> int:
        for sock in self._server.sockets:
            return sock.getsockname()[1]
        return self.config.server.port

    async def stop(self) -> None:
        """Run every farewell, disconnect every device, close every client."""
        if self._stopping:
            return
        self._stopping = True
        log.info("shutting down: %d device(s), %d client(s)", len(self.devices), len(self.clients))
        await asyncio.gather(*(self._release_device(d, "radio") for d in list(self.devices.values())), return_exceptions=True)
        for client in list(self.clients):
            self._spawn(self._close_ws(client, CLOSE_GOING_AWAY, "shutting down"))
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        for task in list(self._tasks):
            task.cancel()
        try:
            await self.backend.close()
        except Exception as exc:  # pragma: no cover
            log.debug("backend close: %s", exc)

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- status -------------------------------------------------------------

    def instance_json(self) -> dict[str, Any]:
        return {"name": self.config.name, "host": socket.gethostname(), "software": f"gattway {__version__}"}

    def radios_json(self) -> list[dict[str, Any]]:
        return [r.to_json() for r in self.radios.values()]

    def devices_json(self) -> list[dict[str, Any]]:
        return [d.to_json() for d in self.devices.values()]

    def hello_fields(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "version": PROTOCOL_VERSION,
            "t": time.time(),
            "instance": self.instance_json(),
            "radios": self.radios_json(),
            "devices": self.devices_json(),
        }

    def status_json(self) -> dict[str, Any]:
        return {
            "type": "status",
            "t": time.time(),
            "instance": self.instance_json(),
            "radios": self.radios_json(),
            "devices": self.devices_json(),
            "clients": [c.to_json() for c in self.clients],
        }

    def changed(self) -> None:
        """Broadcast status soon. Bursts of changes coalesce into one frame."""
        if self._status_pending or self._stopping:
            return
        self._status_pending = True
        asyncio.get_running_loop().call_soon(self._flush_status)

    def _flush_status(self) -> None:
        self._status_pending = False
        self.broadcast_status()

    def broadcast_status(self) -> None:
        status = self.status_json()
        for client in self.clients:
            client.send(status)

    async def _periodic(self) -> None:
        interval = self.config.server.status_interval_s
        tick = min(self.config.server.tick_s, interval)
        last_status = time.monotonic()
        while True:
            await asyncio.sleep(tick)
            now = time.monotonic()
            self._check_heartbeats()
            if now - last_status >= interval:
                last_status = now
                try:
                    await self.refresh_radios()
                except Exception as exc:
                    log.warning("radio refresh failed: %s", exc)
                self.broadcast_status()

    def _check_heartbeats(self) -> None:
        deadline = time.time() - self.config.server.hb_timeout_s
        for client in list(self.clients):
            if client.last_hb < deadline:
                log.info("client %s lost: no heartbeat", client.name)
                self._spawn(self._client_gone(client, CLOSE_HEARTBEAT_LOST, "heartbeat lost"))

    # -- radios -------------------------------------------------------------

    async def refresh_radios(self) -> None:
        present = await self.backend.list_radios()
        by_address = {normalise_address(r.address): r for r in present}
        configured = self.config.radios
        new: dict[str, RadioState] = {}
        for rc in configured:
            state = self.radios.get(rc.address) or RadioState(
                address=rc.address, label=rc.label, enabled=rc.enabled, default=False, configured=True
            )
            state.radio = by_address.pop(rc.address, None)
            new[rc.address] = state
        default_cfg = self.config.default_radio
        for address, radio in by_address.items():
            state = self.radios.get(address) or RadioState(
                address=address, label=None, enabled=not configured, default=False, configured=False
            )
            state.radio = radio
            new[address] = state
        # Default: the configured default, else (nothing configured) the first present radio.
        for state in new.values():
            state.default = False
        if default_cfg is not None:
            new[default_cfg.address].default = True
        elif not configured and new:
            next(iter(new.values())).default = True
        for state in new.values():
            if state.radio is None:
                state.hci = state.bus = None
                state.powered = state.blocked = None
                continue
            try:
                info = await state.radio.info()
            except Exception as exc:
                log.debug("radio %s info: %s", state.address, exc)
                continue
            state.hci, state.bus = info.hci, info.bus
            state.powered, state.blocked = info.powered, info.blocked
        if set(new) != set(self.radios) or any(
            (new[a].present, new[a].hci) != (self.radios[a].present, self.radios[a].hci) for a in new if a in self.radios
        ):
            self.changed()
        self.radios = new

    def resolve_radio(self, which: str | None) -> RadioState:
        if which is None:
            for state in self.radios.values():
                if state.default:
                    if not state.present:
                        raise GattwayError(NO_RADIO, f"default radio {state.name} is not present")
                    if not state.enabled:
                        raise GattwayError(NO_RADIO, f"default radio {state.name} is not enabled")
                    return state
            raise GattwayError(NO_RADIO, "no default radio")
        for state in self.radios.values():
            if state.label == which or state.address == normalise_address(which):
                if not state.present:
                    raise GattwayError(NO_RADIO, f"radio {which} is not present")
                if not state.enabled:
                    raise GattwayError(NO_RADIO, f"radio {which} is not enabled")
                return state
        raise GattwayError(NO_RADIO, f"no radio {which}")

    # -- connections --------------------------------------------------------

    def _authorised(self, ws: Any) -> bool:
        token = self.config.server.token
        if not token:
            return True
        try:
            query = parse_qs(urlsplit(_request_path(ws)).query)
        except Exception:
            return False
        given = query.get("token", [""])[0]
        return hmac.compare_digest(given.encode(), token.encode())

    async def _handler(self, ws: Any) -> None:
        if not self._authorised(ws):
            err = GattwayError(UNAUTHORISED, "a valid ?token= is required")
            try:
                await ws.send(json.dumps({"type": "reply", "id": None, "ok": False, "error": err.to_json()}))
                await ws.close(CLOSE_UNAUTHORISED, "unauthorised")
            except ConnectionClosed:
                pass
            return
        if self._stopping:
            await ws.close(CLOSE_GOING_AWAY, "shutting down")
            return
        self._client_seq += 1
        now = time.time()
        client = ClientState(ws=ws, name=f"client-{self._client_seq}", connected_at=now, last_hb=now)
        client.writer = asyncio.get_running_loop().create_task(self._writer(client))
        self.clients.append(client)
        client.send({"type": "hello", **self.hello_fields()})
        self.changed()
        try:
            async for raw in ws:
                self._on_frame(client, raw)
        except ConnectionClosed:
            pass
        finally:
            await self._client_gone(client, None, None)

    async def _writer(self, client: ClientState) -> None:
        try:
            while True:
                frame = await client.outbox.get()
                await client.ws.send(frame)
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as exc:  # pragma: no cover
            log.debug("writer for %s: %s", client.name, exc)

    async def _close_ws(self, client: ClientState, code: int, reason: str) -> None:
        try:
            await asyncio.wait_for(client.ws.close(code, reason), 2.0)
        except Exception:
            pass

    async def _client_gone(self, client: ClientState, code: int | None, reason: str | None) -> None:
        """The client's socket closed or its heartbeat stopped. Idempotent."""
        if client.gone:
            return
        client.gone = True
        if client in self.clients:
            self.clients.remove(client)
        owned = [d for d in self.devices.values() if d.owner is client]
        if owned:
            log.info("client %s gone; releasing %d device(s)", client.name, len(owned))
        await asyncio.gather(*(self._release_device(d, "owner_lost") for d in owned), return_exceptions=True)
        for task in list(client.tasks):
            task.cancel()
        if client.writer is not None:
            await asyncio.sleep(0)  # let the writer flush what it already holds
            client.writer.cancel()
        if code is not None:
            await self._close_ws(client, code, reason or "")
        self.changed()

    def _on_frame(self, client: ClientState, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            client.send(self._error_reply(None, GattwayError(BAD_REQUEST, "frame is not JSON")))
            return
        if not isinstance(msg, dict):
            client.send(self._error_reply(None, GattwayError(BAD_REQUEST, "frame must be an object")))
            return
        if msg.get("type") == "hb":
            client.last_hb = time.time()
            reply: dict[str, Any] = {"type": "hb", "t": client.last_hb}
            if "t" in msg:
                reply["echo"] = msg["t"]
            client.send(reply)
            return
        if "op" in msg:
            task = asyncio.get_running_loop().create_task(self._handle_request(client, msg))
            client.tasks.add(task)
            task.add_done_callback(client.tasks.discard)
            return
        client.send(self._error_reply(msg.get("id"), GattwayError(BAD_REQUEST, "expected op or type")))

    @staticmethod
    def _error_reply(req_id: Any, err: GattwayError) -> dict[str, Any]:
        return {"type": "reply", "id": req_id, "ok": False, "error": err.to_json()}

    async def _handle_request(self, client: ClientState, msg: dict[str, Any]) -> None:
        req_id = msg.get("id")
        op = msg.get("op")
        post: Awaitable[Any] | None = None
        try:
            handler = self._ops.get(op) if isinstance(op, str) else None
            if handler is None:
                raise GattwayError(UNKNOWN_OP, f"unknown op {op!r}")
            result = await handler(client, msg)
            if isinstance(result, tuple):
                result, post = result
            client.send({"type": "reply", "id": req_id, "ok": True, "result": result})
        except GattwayError as err:
            client.send(self._error_reply(req_id, err))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("op %s failed", op)
            client.send(self._error_reply(req_id, GattwayError("error", f"{type(exc).__name__}: {exc}")))
        if post is not None:
            await post

    # -- ops ----------------------------------------------------------------

    async def _op_hello(self, client: ClientState, msg: dict[str, Any]) -> Any:
        name = _str_arg(msg, "name")
        protocol = msg.get("protocol", PROTOCOL)
        version = msg.get("version", PROTOCOL_VERSION)
        if protocol != PROTOCOL:
            raise GattwayError(BAD_REQUEST, f"protocol must be {PROTOCOL!r}")
        if isinstance(version, bool) or not isinstance(version, (int, float)) or int(version) != PROTOCOL_VERSION:
            raise GattwayError(BAD_REQUEST, f"unsupported protocol version {version!r}; this instance speaks {PROTOCOL_VERSION}")
        client.name = name  # type: ignore[assignment]
        self.changed()
        return self.hello_fields()

    async def _op_radios(self, client: ClientState, msg: dict[str, Any]) -> Any:
        await self.refresh_radios()
        return {"radios": self.radios_json()}

    async def _op_scan(self, client: ClientState, msg: dict[str, Any]) -> Any:
        rs = self.resolve_radio(_str_arg(msg, "radio", required=False))
        timeout_s = _number_arg(msg, "timeout_s", SCAN_DEFAULT_S, SCAN_MIN_S, SCAN_MAX_S)
        name_prefix = _str_arg(msg, "name_prefix", required=False)
        services_arg = msg.get("services")
        services: list[str] | None = None
        if services_arg is not None:
            if not isinstance(services_arg, list):
                raise GattwayError(BAD_REQUEST, "services must be a list of uuids")
            services = [expand_uuid(s) for s in services_arg]
        if rs.lock.locked():
            raise GattwayError(BUSY, f"radio {rs.name} is busy")
        async with rs.lock:
            rs.scanning = True
            self.changed()
            try:
                found = await rs.radio.scan(timeout_s, name_prefix, services)  # type: ignore[union-attr]
            finally:
                rs.scanning = False
                self.changed()
        return {"devices": [d.to_json(rs.name) for d in found]}

    async def _op_connect(self, client: ClientState, msg: dict[str, Any]) -> Any:
        address = _address_arg(msg)
        rs = self.resolve_radio(_str_arg(msg, "radio", required=False))
        timeout_s = _number_arg(msg, "timeout_s", CONNECT_DEFAULT_S, 1.0, 120.0)
        existing = self.devices.get(address)
        if existing is not None:
            if existing.owner is client:
                return self._connect_result(existing)
            raise GattwayError(HELD, f"connected by {existing.owner.name}", {"owner": existing.owner.name})
        if rs.lock.locked():
            raise GattwayError(BUSY, f"radio {rs.name} is busy")
        async with rs.lock:
            existing = self.devices.get(address)
            if existing is not None:
                raise GattwayError(HELD, f"connected by {existing.owner.name}", {"owner": existing.owner.name})
            loop = asyncio.get_running_loop()

            def on_notify(char: str, data: bytes) -> None:
                dev = self.devices.get(address)
                if dev is not None:
                    dev.owner.send({"type": "notify", "address": address, "char": char, "data": data.hex(), "t": time.time()})

            def on_disconnect(reason: str) -> None:
                dev = self.devices.get(address)
                if dev is not None:
                    loop.create_task(self._device_ended(dev, reason, requested=False))

            try:
                link = await rs.radio.connect(address, timeout_s, on_notify, on_disconnect)  # type: ignore[union-attr]
            except GattwayError:
                raise
            except asyncio.TimeoutError:
                raise GattwayError("timeout", f"connect to {address} timed out") from None
            except Exception as exc:
                raise GattwayError("connect_failed", str(exc)) from exc
            dev = DeviceState(
                address=address,
                name=link.name,
                radio=rs,
                link=link,
                owner=client,
                connected_at=time.time(),
                queue=TimedQueue(link, self.config.server.late_ms / 1000.0),
            )
            self.devices[address] = dev
        log.info("%s connected %s on %s", client.name, address, rs.name)
        self.changed()
        return self._connect_result(dev)

    @staticmethod
    def _connect_result(dev: DeviceState) -> dict[str, Any]:
        return {"address": dev.address, "name": dev.name, "radio": dev.radio.name, "services": dev.link.services_json()}

    def _owned(self, client: ClientState, msg: dict[str, Any]) -> DeviceState:
        address = _address_arg(msg)
        dev = self.devices.get(address)
        if dev is None:
            raise GattwayError(NOT_CONNECTED, f"{address} is not connected")
        if dev.owner is not client:
            raise GattwayError(NOT_OWNER, f"{address} belongs to {dev.owner.name}", {"owner": dev.owner.name})
        return dev

    async def _op_disconnect(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        dev.queue.stop()
        try:
            await dev.link.disconnect()
        except Exception as exc:
            log.debug("disconnect %s: %s", dev.address, exc)
        # Reply first, then the `disconnected` message and status.
        return {}, self._device_ended(dev, "requested", requested=True)

    async def _op_farewell(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        writes = msg.get("writes")
        if not isinstance(writes, list):
            raise GattwayError(BAD_REQUEST, "writes must be a list")
        parsed = []
        for w in writes:
            if not isinstance(w, dict):
                raise GattwayError(BAD_REQUEST, "each write must be an object")
            parsed.append({"char": _char_arg(w), "data": _hex_arg(w.get("data")), "response": _bool_arg(w, "response")})
        dev.farewell = parsed
        self.changed()
        return {}

    async def _op_write(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        char = _char_arg(msg)
        data = _hex_arg(msg.get("data"))
        response = _bool_arg(msg, "response")
        await dev.link.write(char, data, response)
        return {}

    async def _op_write_at(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        char = _char_arg(msg)
        response = _bool_arg(msg, "response")
        frames = msg.get("frames")
        if not isinstance(frames, list):
            raise GattwayError(BAD_REQUEST, "frames must be a list")
        parsed: list[tuple[float, bytes]] = []
        for f in frames:
            if not isinstance(f, dict):
                raise GattwayError(BAD_REQUEST, "each frame must be an object")
            t = f.get("t")
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                raise GattwayError(BAD_REQUEST, "frame t must be a number")
            parsed.append((float(t), _hex_arg(f.get("data"))))
        return dev.queue.add(char, parsed, response)

    async def _op_cancel(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        char = _str_arg(msg, "char", required=False)
        return {"cancelled": dev.queue.cancel(expand_uuid(char) if char else None)}

    async def _op_read(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        data = await dev.link.read(_char_arg(msg))
        return {"data": bytes(data).hex()}

    async def _op_subscribe(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        char = _char_arg(msg)
        await dev.link.subscribe(char)
        dev.subscribed.add(char)
        self.changed()
        return {}

    async def _op_unsubscribe(self, client: ClientState, msg: dict[str, Any]) -> Any:
        dev = self._owned(client, msg)
        char = _char_arg(msg)
        await dev.link.unsubscribe(char)
        dev.subscribed.discard(char)
        self.changed()
        return {}

    # -- device teardown ----------------------------------------------------

    async def _release_device(self, dev: DeviceState, reason: str) -> None:
        """Farewell writes in order, then disconnect (section 5.4 and 5.5)."""
        if self.devices.get(dev.address) is not dev:
            return
        dev.queue.stop()
        if dev.farewell:
            log.info("farewell for %s: %d write(s)", dev.address, len(dev.farewell))
        for i, w in enumerate(dev.farewell):
            if i:
                await asyncio.sleep(self.config.server.farewell_gap_s)
            try:
                await dev.link.write(w["char"], w["data"], w["response"])
            except Exception as exc:
                log.warning("farewell write to %s failed: %s", dev.address, exc)
        try:
            await dev.link.disconnect()
        except Exception as exc:
            log.debug("disconnect %s: %s", dev.address, exc)
        await self._device_ended(dev, reason, requested=False)

    async def _device_ended(self, dev: DeviceState, reason: str, requested: bool) -> None:
        if self.devices.get(dev.address) is not dev:
            return
        del self.devices[dev.address]
        dev.queue.stop()
        log.info("%s disconnected (%s)", dev.address, reason)
        dev.owner.send(
            {"type": "disconnected", "address": dev.address, "reason": reason, "requested": requested, "t": time.time()}
        )
        self.changed()


# ----------------------------------------------------------------------------
# Entry points


async def make_backend(config: Config, radio_factory: RadioFactory | None = None) -> RadioBackend:
    if radio_factory is not None:
        backend = radio_factory(config)
        if inspect.isawaitable(backend):
            backend = await backend
        return backend  # type: ignore[return-value]
    if config.fake:
        from .radios.fake import FakeBackend

        return FakeBackend()
    from .radios.bleak_radio import BleakBackend

    return BleakBackend()


async def serve(config: Config, radio_factory: RadioFactory | None = None) -> None:
    """Run an instance until SIGTERM or SIGINT.

    ``radio_factory(config)`` may return a :class:`RadioBackend` (or a coroutine
    producing one) to embed gattway around your own radios.
    """
    backend = await make_backend(config, radio_factory)
    server = GattwayServer(config, backend)
    await server.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass
    try:
        await stop.wait()
    finally:
        await server.stop()
