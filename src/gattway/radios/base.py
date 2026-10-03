"""The radio abstraction the server is written against."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

NotifyCallback = Callable[[str, bytes], None]
DisconnectCallback = Callable[[str], None]  # reason: peer|radio|error


@dataclass
class RadioInfo:
    """What the host knows about one radio."""

    address: str
    hci: str | None = None
    bus: str | None = None  # "usb", "uart", or None when unknown
    powered: bool | None = None
    blocked: bool | None = None


@dataclass
class ScannedDevice:
    address: str
    name: str | None
    rssi: int | None
    services: list[str] = field(default_factory=list)
    manufacturer: dict[str, str] = field(default_factory=dict)  # company id -> hex

    def to_json(self, radio: str | None) -> dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "rssi": self.rssi,
            "radio": radio,
            "services": list(self.services),
            "manufacturer": dict(self.manufacturer),
        }


@dataclass
class Characteristic:
    uuid: str
    properties: list[str]

    def to_json(self) -> dict[str, Any]:
        return {"uuid": self.uuid, "properties": list(self.properties)}


@dataclass
class Service:
    uuid: str
    characteristics: list[Characteristic] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"uuid": self.uuid, "characteristics": [c.to_json() for c in self.characteristics]}


class DeviceLink(ABC):
    """A connected device. One per connection; thrown away after disconnect."""

    address: str
    name: str | None
    services: list[Service]

    @abstractmethod
    async def read(self, char: str) -> bytes: ...

    @abstractmethod
    async def write(self, char: str, data: bytes, response: bool = False) -> None: ...

    @abstractmethod
    async def subscribe(self, char: str) -> None: ...

    @abstractmethod
    async def unsubscribe(self, char: str) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    def services_json(self) -> list[dict[str, Any]]:
        return [s.to_json() for s in self.services]


class Radio(ABC):
    """One Bluetooth adapter."""

    address: str
    hci: str | None
    bus: str | None

    @abstractmethod
    async def info(self) -> RadioInfo:
        """Current powered/blocked state. Cheap; called every few seconds."""

    @abstractmethod
    async def scan(
        self,
        timeout_s: float,
        name_prefix: str | None = None,
        services: list[str] | None = None,
    ) -> list[ScannedDevice]:
        """Scan for ``timeout_s`` seconds. Returns devices strongest first."""

    @abstractmethod
    async def connect(
        self,
        address: str,
        timeout_s: float,
        on_notify: NotifyCallback,
        on_disconnect: DisconnectCallback,
    ) -> DeviceLink:
        """Connect and discover services. Raises GattwayError on failure.

        ``on_notify(char_uuid, data)`` is called for every notification of a
        subscribed characteristic. ``on_disconnect(reason)`` is called once if
        the link ends without ``DeviceLink.disconnect`` having been called.
        """


class RadioBackend(ABC):
    """Finds the radios on this host."""

    @abstractmethod
    async def list_radios(self) -> list[Radio]:
        """Every radio present right now, in a stable order."""

    async def close(self) -> None:
        """Release anything held. Called once on shutdown."""


RadioFactory = Callable[[Any], RadioBackend | Awaitable[RadioBackend]]
