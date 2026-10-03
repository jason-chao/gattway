"""A scripted fake radio for tests and development.

The default fake device ``fake-echo`` (``00:fa:ke:00:00:01``) has one service:

- ``fa01`` read / write / write-without-response: every frame written is
  echoed back as a notification on ``fa02``; reading returns the last frame.
- ``fa02`` notify: the echo.
- ``fa03`` notify: a 2-byte big-endian counter at 10 Hz while subscribed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import (
    CONNECT_FAILED,
    NO_CHARACTERISTIC,
    NOT_FOUND,
    READ_FAILED,
    WRITE_FAILED,
    GattwayError,
)
from ..uuids import expand_uuid
from .base import (
    Characteristic,
    DeviceLink,
    DisconnectCallback,
    NotifyCallback,
    Radio,
    RadioBackend,
    RadioInfo,
    ScannedDevice,
    Service,
)

FAKE_RADIO_ADDRESS = "00:fa:ke:00:00:00"
FAKE_DEVICE_ADDRESS = "00:fa:ke:00:00:01"
FAKE_DEVICE_NAME = "fake-echo"

FAKE_SERVICE = expand_uuid("fa00")
FAKE_WRITE_CHAR = expand_uuid("fa01")
FAKE_ECHO_CHAR = expand_uuid("fa02")
FAKE_COUNTER_CHAR = expand_uuid("fa03")

COUNTER_HZ = 10.0

WriteHandler = Callable[["FakeDevice", str, bytes, bool], None]


@dataclass
class FakeCharacteristic:
    uuid: str
    properties: list[str]
    value: bytes = b""
    on_write: WriteHandler | None = None
    # Called with (device, link) when subscribed; returns a coroutine function
    # that emits notifications until cancelled, or None.
    stream: Callable[["FakeDevice", str], Any] | None = None


@dataclass
class WriteRecord:
    t: float
    char: str
    data: bytes
    response: bool


@dataclass
class FakeDevice:
    """A scripted peripheral. Build one with :func:`echo_device` or by hand."""

    address: str
    name: str | None
    rssi: int = -50
    service_uuid: str = FAKE_SERVICE
    chars: dict[str, FakeCharacteristic] = field(default_factory=dict)
    manufacturer: dict[str, str] = field(default_factory=dict)
    connect_delay_s: float = 0.02
    writes: list[WriteRecord] = field(default_factory=list)
    link: "FakeLink | None" = None

    def add_char(self, char: FakeCharacteristic) -> None:
        self.chars[expand_uuid(char.uuid)] = char

    @property
    def connected(self) -> bool:
        return self.link is not None

    def services(self) -> list[Service]:
        return [
            Service(
                uuid=self.service_uuid,
                characteristics=[Characteristic(uuid=u, properties=list(c.properties)) for u, c in self.chars.items()],
            )
        ]

    def notify(self, char: str, data: bytes) -> None:
        """Emit a notification if someone is connected and subscribed."""
        if self.link is not None:
            self.link.deliver(expand_uuid(char), data)

    def drop(self, reason: str = "peer") -> None:
        """Simulate the device dropping the link."""
        if self.link is not None:
            self.link._ended(reason)

    def scanned(self) -> ScannedDevice:
        return ScannedDevice(
            address=self.address,
            name=self.name,
            rssi=self.rssi,
            services=[self.service_uuid],
            manufacturer=dict(self.manufacturer),
        )


def _echo_write(device: FakeDevice, char: str, data: bytes, response: bool) -> None:
    device.notify(FAKE_ECHO_CHAR, data)


async def _counter_stream(device: FakeDevice, char: str) -> None:
    n = 0
    period = 1.0 / COUNTER_HZ
    next_t = time.monotonic() + period
    while True:
        await asyncio.sleep(max(0.0, next_t - time.monotonic()))
        next_t += period
        device.notify(char, (n & 0xFFFF).to_bytes(2, "big"))
        n += 1


def echo_device(address: str = FAKE_DEVICE_ADDRESS, name: str = FAKE_DEVICE_NAME) -> FakeDevice:
    dev = FakeDevice(address=address, name=name)
    dev.add_char(
        FakeCharacteristic(
            uuid=FAKE_WRITE_CHAR,
            properties=["read", "write", "write-without-response"],
            on_write=_echo_write,
        )
    )
    dev.add_char(FakeCharacteristic(uuid=FAKE_ECHO_CHAR, properties=["notify"]))
    dev.add_char(FakeCharacteristic(uuid=FAKE_COUNTER_CHAR, properties=["notify"], stream=_counter_stream))
    return dev


class FakeLink(DeviceLink):
    def __init__(self, device: FakeDevice, on_notify: NotifyCallback, on_disconnect: DisconnectCallback):
        self.device = device
        self.address = device.address
        self.name = device.name
        self.services = device.services()
        self._on_notify = on_notify
        self._on_disconnect = on_disconnect
        self._subscribed: set[str] = set()
        self._streams: dict[str, asyncio.Task] = {}
        self._open = True

    def _char(self, char: str) -> FakeCharacteristic:
        uuid = expand_uuid(char)
        try:
            return self.device.chars[uuid]
        except KeyError:
            raise GattwayError(NO_CHARACTERISTIC, f"no characteristic {uuid}") from None

    def _check_open(self, code: str) -> None:
        if not self._open:
            raise GattwayError(code, "link is closed")

    async def read(self, char: str) -> bytes:
        self._check_open(READ_FAILED)
        c = self._char(char)
        if "read" not in c.properties:
            raise GattwayError(READ_FAILED, "characteristic is not readable")
        return c.value

    async def write(self, char: str, data: bytes, response: bool = False) -> None:
        self._check_open(WRITE_FAILED)
        c = self._char(char)
        if "write" not in c.properties and "write-without-response" not in c.properties:
            raise GattwayError(WRITE_FAILED, "characteristic is not writable")
        c.value = bytes(data)
        self.device.writes.append(WriteRecord(time.time(), c.uuid, bytes(data), response))
        if c.on_write is not None:
            c.on_write(self.device, c.uuid, bytes(data), response)

    async def subscribe(self, char: str) -> None:
        self._check_open(WRITE_FAILED)
        c = self._char(char)
        if "notify" not in c.properties and "indicate" not in c.properties:
            raise GattwayError(WRITE_FAILED, "characteristic does not notify")
        if c.uuid in self._subscribed:
            return
        self._subscribed.add(c.uuid)
        if c.stream is not None:
            self._streams[c.uuid] = asyncio.get_running_loop().create_task(c.stream(self.device, c.uuid))

    async def unsubscribe(self, char: str) -> None:
        c = self._char(char)
        self._subscribed.discard(c.uuid)
        task = self._streams.pop(c.uuid, None)
        if task is not None:
            task.cancel()

    async def disconnect(self) -> None:
        if not self._open:
            return
        self._close()

    def deliver(self, uuid: str, data: bytes) -> None:
        if self._open and uuid in self._subscribed:
            self._on_notify(uuid, bytes(data))

    def _close(self) -> None:
        self._open = False
        for task in self._streams.values():
            task.cancel()
        self._streams.clear()
        self._subscribed.clear()
        if self.device.link is self:
            self.device.link = None

    def _ended(self, reason: str) -> None:
        if not self._open:
            return
        self._close()
        self._on_disconnect(reason)


class FakeRadio(Radio):
    def __init__(self, devices: list[FakeDevice], address: str = FAKE_RADIO_ADDRESS, hci: str = "fake0"):
        self.address = address
        self.hci = hci
        self.bus = "fake"
        self.devices = devices
        self.scan_sleep = True  # tests can turn the real wait off

    async def info(self) -> RadioInfo:
        return RadioInfo(address=self.address, hci=self.hci, bus=self.bus, powered=True, blocked=False)

    async def scan(self, timeout_s: float, name_prefix: str | None = None, services: list[str] | None = None):
        if self.scan_sleep:
            await asyncio.sleep(timeout_s)
        wanted = {expand_uuid(s) for s in services} if services else None
        found = []
        for dev in self.devices:
            if name_prefix and not (dev.name or "").startswith(name_prefix):
                continue
            if wanted and not (wanted & {dev.service_uuid}):
                continue
            found.append(dev.scanned())
        found.sort(key=lambda d: -(d.rssi if d.rssi is not None else -999))
        return found

    async def connect(self, address: str, timeout_s: float, on_notify: NotifyCallback, on_disconnect: DisconnectCallback):
        address = address.lower()
        for dev in self.devices:
            if dev.address.lower() == address:
                break
        else:
            raise GattwayError(NOT_FOUND, f"no device {address}")
        if dev.link is not None:
            raise GattwayError(CONNECT_FAILED, "device already connected")
        await asyncio.sleep(min(dev.connect_delay_s, timeout_s))
        link = FakeLink(dev, on_notify, on_disconnect)
        dev.link = link
        return link


class FakeBackend(RadioBackend):
    """One fake radio holding the given devices (default: one echo device)."""

    def __init__(self, devices: list[FakeDevice] | None = None):
        self.devices = devices if devices is not None else [echo_device()]
        self.radio = FakeRadio(self.devices)

    def device(self, address: str = FAKE_DEVICE_ADDRESS) -> FakeDevice:
        for d in self.devices:
            if d.address.lower() == address.lower():
                return d
        raise KeyError(address)

    async def list_radios(self) -> list[Radio]:
        return [self.radio]
