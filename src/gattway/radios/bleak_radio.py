"""Real radios through bleak on BlueZ (Linux).

Radios are picked by BD address and mapped to an ``hciN`` adapter at call time,
because the numbering changes when a USB radio re-enumerates.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any

from ..errors import (
    BUSY,
    CONNECT_FAILED,
    NO_CHARACTERISTIC,
    NOT_FOUND,
    READ_FAILED,
    TIMEOUT,
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
from .hci import list_host_radios

log = logging.getLogger("gattway.bleak")

CONNECT_RETRIES = 3
RETRY_DELAY_S = 1.0
_RETRY_MARKERS = ("InProgress", "Busy", "in progress", "busy")


def _is_retryable(exc: BaseException) -> bool:
    text = str(exc)
    dbus = getattr(exc, "dbus_error", None)
    if isinstance(dbus, str) and any(m in dbus for m in _RETRY_MARKERS):
        return True
    return any(m in text for m in _RETRY_MARKERS)


def _hex_manufacturer(data: dict[int, bytes]) -> dict[str, str]:
    return {str(k): bytes(v).hex() for k, v in data.items()}


async def _bluetoothctl_disconnect(address: str) -> None:
    """Clear a stale BlueZ connection to ``address``. Errors are ignored."""
    if shutil.which("bluetoothctl") is None:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            "disconnect",
            address.upper(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), 5.0)
    except (OSError, asyncio.TimeoutError):
        pass


class BleakLink(DeviceLink):
    def __init__(self, client: Any, address: str, name: str | None, on_notify: NotifyCallback):
        self.client = client
        self.address = address
        self.name = name
        self._on_notify = on_notify
        self.services = self._collect_services()
        self._subscribed: set[str] = set()
        self.closed_by_us = False

    def _collect_services(self) -> list[Service]:
        out: list[Service] = []
        for svc in self.client.services:
            chars = [
                Characteristic(uuid=expand_uuid(str(c.uuid)), properties=list(c.properties)) for c in svc.characteristics
            ]
            out.append(Service(uuid=expand_uuid(str(svc.uuid)), characteristics=chars))
        return out

    def _char(self, char: str):
        uuid = expand_uuid(char)
        found = self.client.services.get_characteristic(uuid)
        if found is None:
            raise GattwayError(NO_CHARACTERISTIC, f"no characteristic {uuid}")
        return found

    async def read(self, char: str) -> bytes:
        c = self._char(char)
        try:
            return bytes(await self.client.read_gatt_char(c))
        except GattwayError:
            raise
        except asyncio.TimeoutError:
            raise GattwayError(TIMEOUT, "read timed out") from None
        except Exception as exc:  # bleak errors
            raise GattwayError(READ_FAILED, str(exc)) from exc

    async def write(self, char: str, data: bytes, response: bool = False) -> None:
        c = self._char(char)
        try:
            await self.client.write_gatt_char(c, bytes(data), response=response)
        except GattwayError:
            raise
        except asyncio.TimeoutError:
            raise GattwayError(TIMEOUT, "write timed out") from None
        except Exception as exc:
            raise GattwayError(WRITE_FAILED, str(exc)) from exc

    async def subscribe(self, char: str) -> None:
        c = self._char(char)
        uuid = expand_uuid(str(c.uuid))
        if uuid in self._subscribed:
            return

        def cb(_characteristic: Any, data: bytearray) -> None:
            self._on_notify(uuid, bytes(data))

        try:
            await self.client.start_notify(c, cb)
        except Exception as exc:
            raise GattwayError(WRITE_FAILED, f"subscribe failed: {exc}") from exc
        self._subscribed.add(uuid)

    async def unsubscribe(self, char: str) -> None:
        c = self._char(char)
        uuid = expand_uuid(str(c.uuid))
        self._subscribed.discard(uuid)
        try:
            await self.client.stop_notify(c)
        except Exception as exc:
            log.debug("stop_notify %s: %s", uuid, exc)

    async def disconnect(self) -> None:
        self.closed_by_us = True
        try:
            await asyncio.wait_for(self.client.disconnect(), 10.0)
        except Exception as exc:
            log.debug("disconnect %s: %s", self.address, exc)


class BleakRadio(Radio):
    def __init__(self, info: RadioInfo):
        self.address = info.address
        self.hci = info.hci
        self.bus = info.bus
        self._info = info

    def _update(self, info: RadioInfo) -> None:
        self._info = info
        self.hci = info.hci
        self.bus = info.bus

    async def info(self) -> RadioInfo:
        return self._info

    def _adapter_kwargs(self) -> dict[str, Any]:
        return {"adapter": self.hci} if self.hci else {}

    async def scan(self, timeout_s: float, name_prefix: str | None = None, services: list[str] | None = None):
        from bleak import BleakScanner  # lazy: keep import cost off the fake path

        wanted = [expand_uuid(s) for s in services] if services else None
        seen: dict[str, ScannedDevice] = {}

        def on_adv(device: Any, adv: Any) -> None:
            name = adv.local_name or device.name
            if name_prefix and not (name or "").startswith(name_prefix):
                return
            seen[device.address.lower()] = ScannedDevice(
                address=device.address.lower(),
                name=name,
                rssi=adv.rssi,
                services=[expand_uuid(u) for u in (adv.service_uuids or [])],
                manufacturer=_hex_manufacturer(adv.manufacturer_data or {}),
            )

        try:
            scanner = BleakScanner(detection_callback=on_adv, service_uuids=wanted, **self._adapter_kwargs())
            async with scanner:
                await asyncio.sleep(timeout_s)
        except Exception as exc:
            if _is_retryable(exc):
                raise GattwayError(BUSY, f"radio busy: {exc}") from exc
            raise GattwayError(NOT_FOUND, f"scan failed: {exc}") from exc
        found = list(seen.values())
        found.sort(key=lambda d: -(d.rssi if d.rssi is not None else -999))
        return found

    async def connect(self, address: str, timeout_s: float, on_notify: NotifyCallback, on_disconnect: DisconnectCallback):
        from bleak import BleakClient
        from bleak.exc import BleakDeviceNotFoundError

        address = address.lower()
        await _bluetoothctl_disconnect(address)

        link_box: list[BleakLink] = []

        def disconnected(_client: Any) -> None:
            if link_box and not link_box[0].closed_by_us:
                on_disconnect("peer")

        deadline = time.monotonic() + timeout_s
        last_exc: BaseException | None = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            client = BleakClient(
                address.upper(), disconnected_callback=disconnected, timeout=remaining, **self._adapter_kwargs()
            )
            try:
                await client.connect()
            except BleakDeviceNotFoundError as exc:
                raise GattwayError(NOT_FOUND, str(exc)) from exc
            except asyncio.TimeoutError:
                raise GattwayError(TIMEOUT, f"connect to {address} timed out") from None
            except Exception as exc:
                last_exc = exc
                if _is_retryable(exc) and attempt < CONNECT_RETRIES:
                    log.info("connect %s attempt %d: %s; retrying", address, attempt, exc)
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue
                raise GattwayError(CONNECT_FAILED, str(exc)) from exc
            name = None
            try:
                dev = getattr(client, "_device_info", None)
                if isinstance(dev, dict):
                    name = dev.get("Name") or dev.get("Alias")
            except Exception:
                name = None
            link = BleakLink(client, address, name, on_notify)
            link_box.append(link)
            return link
        raise GattwayError(CONNECT_FAILED, f"connect to {address} failed: {last_exc}")


class BleakBackend(RadioBackend):
    def __init__(self) -> None:
        self._radios: dict[str, BleakRadio] = {}

    async def list_radios(self) -> list[Radio]:
        infos = await list_host_radios()
        out: list[Radio] = []
        for info in infos:
            radio = self._radios.get(info.address)
            if radio is None:
                radio = BleakRadio(info)
                self._radios[info.address] = radio
            else:
                radio._update(info)
            out.append(radio)
        return out
