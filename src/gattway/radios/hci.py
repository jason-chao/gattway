"""Host radio enumeration on Linux/BlueZ.

Sources, in order of preference: sysfs for presence and bus, BlueZ over D-Bus
(``org.bluez.Adapter1``: address and power; available wherever ``bluetoothd``
runs), ``hciconfig`` as a fallback (deprecated and absent on some
distributions), and ``rfkill`` for blocking. Every source is optional.

The parsers are pure functions so they can be unit-tested on sample data.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .base import RadioInfo

SYSFS_BLUETOOTH = Path("/sys/class/bluetooth")

_HCI_HEADER = re.compile(r"^(hci\d+):", re.MULTILINE)
_BD_ADDRESS = re.compile(r"BD Address:\s*([0-9A-Fa-f:]{17})")
_ADDR_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


@dataclass
class HciConfigEntry:
    hci: str
    address: str | None
    up: bool | None
    bus: str | None


def parse_hciconfig(text: str) -> dict[str, HciConfigEntry]:
    """Parse the output of ``hciconfig -a`` (or plain ``hciconfig``).

    Returns a mapping ``hciN -> HciConfigEntry``. Example block::

        hci0:   Type: Primary  Bus: USB
                BD Address: 00:11:22:33:44:55  ACL MTU: 1021:8  SCO MTU: 64:1
                UP RUNNING
                RX bytes:1234 acl:0 sco:0 events:56 errors:0
    """
    entries: dict[str, HciConfigEntry] = {}
    headers = list(_HCI_HEADER.finditer(text))
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end]
        hci = m.group(1)
        addr_m = _BD_ADDRESS.search(block)
        address = addr_m.group(1).lower() if addr_m else None
        bus_m = re.search(r"Bus:\s*(\w+)", block)
        bus = _normalise_bus(bus_m.group(1)) if bus_m else None
        up: bool | None
        flags_line = None
        for line in block.splitlines()[1:]:
            stripped = line.strip()
            if stripped and stripped.split()[0] in ("UP", "DOWN"):
                flags_line = stripped
                break
        if flags_line is None:
            up = None
        else:
            up = flags_line.startswith("UP")
        entries[hci] = HciConfigEntry(hci=hci, address=address, up=up, bus=bus)
    return entries


def parse_rfkill_json(text: str) -> dict[str, bool]:
    """Parse ``rfkill -J`` output. Returns ``hciN -> blocked`` (soft or hard)."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    devices: list = []
    if isinstance(doc, dict):
        for value in doc.values():  # key is "rfkilldevices" or "" on old util-linux
            if isinstance(value, list):
                devices.extend(value)
    elif isinstance(doc, list):
        devices = doc
    out: dict[str, bool] = {}
    for dev in devices:
        if not isinstance(dev, dict) or dev.get("type") != "bluetooth":
            continue
        name = dev.get("device")
        if not isinstance(name, str):
            continue
        soft = str(dev.get("soft", "")).lower() == "blocked"
        hard = str(dev.get("hard", "")).lower() == "blocked"
        out[name] = soft or hard
    return out


def parse_rfkill_list(text: str) -> dict[str, bool]:
    """Parse ``rfkill list`` output (fallback when ``-J`` is unsupported)."""
    out: dict[str, bool] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^\d+:\s*(\S+):\s*(.*)$", line)
        if m:
            current = m.group(1) if m.group(2).strip().lower() == "bluetooth" else None
            if current:
                out[current] = False
            continue
        if current:
            sm = re.match(r"^\s*(Soft|Hard) blocked:\s*(yes|no)", line)
            if sm and sm.group(2) == "yes":
                out[current] = True
    return out


def parse_bluez_objects(objects: dict) -> dict[str, tuple[str | None, bool | None]]:
    """Adapters from BlueZ's ``ObjectManager.GetManagedObjects`` result.

    ``objects`` maps object paths to ``{interface: {property: value}}``; values
    may be plain or wrapped in an object with a ``.value`` attribute (dbus-fast
    ``Variant``). Returns ``hciN -> (address, powered)``.
    """
    out: dict[str, tuple[str | None, bool | None]] = {}
    for path, interfaces in (objects or {}).items():
        props = (interfaces or {}).get("org.bluez.Adapter1")
        if props is None:
            continue
        name = str(path).rsplit("/", 1)[-1]
        if not re.fullmatch(r"hci\d+", name):
            continue
        address = _plain(props.get("Address"))
        powered = _plain(props.get("Powered"))
        address = str(address).lower() if address and _ADDR_RE.match(str(address).lower()) else None
        out[name] = (address, bool(powered) if powered is not None else None)
    return out


def _plain(value):
    return getattr(value, "value", value)


async def _bluez_adapters(timeout: float = 2.0) -> dict[str, tuple[str | None, bool | None]]:
    """Ask bluetoothd for its adapters over the system bus. Empty on any failure."""
    try:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
    except ImportError:  # not on Linux, or bleak without its D-Bus backend
        return {}
    bus = None
    try:
        bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), timeout)
        intro = await asyncio.wait_for(bus.introspect("org.bluez", "/"), timeout)
        obj = bus.get_proxy_object("org.bluez", "/", intro)
        manager = obj.get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await asyncio.wait_for(manager.call_get_managed_objects(), timeout)
        return parse_bluez_objects(objects)
    except Exception:  # noqa: BLE001 - no system bus, no bluetoothd, no permission: just skip
        return {}
    finally:
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass


def bus_from_device_path(path: str) -> str | None:
    """Guess the bus from a resolved ``/sys/class/bluetooth/hciN/device`` path."""
    p = path.lower()
    if "/usb" in p:
        return "usb"
    if any(tag in p for tag in ("/serial", "/tty", "uart", "/amba", "/platform")):
        return "uart"
    if "/pci" in p:
        return "pci"
    if "/sdio" in p or "/mmc" in p:
        return "sdio"
    return None


def _normalise_bus(bus: str) -> str | None:
    b = bus.lower()
    if b in ("usb", "uart", "pci", "sdio"):
        return b
    return b or None


async def _run(*argv: str, timeout: float = 3.0) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    return out.decode(errors="replace")


async def list_host_radios(sysfs: Path = SYSFS_BLUETOOTH) -> list[RadioInfo]:
    """Enumerate radios: sysfs for presence and bus, BlueZ D-Bus (then
    hciconfig) for addresses and power, rfkill for blocking. Any source that is
    missing is skipped."""
    hcis = sorted(
        (p.name for p in sysfs.glob("hci*") if p.name[3:].isdigit()),
        key=lambda n: int(n[3:]),
    )
    if not hcis:
        return []
    bluez, hciconfig_text, rfkill_text = await asyncio.gather(
        _bluez_adapters(), _run("hciconfig", "-a"), _run("rfkill", "-J")
    )
    hci_entries = parse_hciconfig(hciconfig_text) if hciconfig_text else {}
    blocked = parse_rfkill_json(rfkill_text) if rfkill_text else {}
    if not blocked:
        listing = await _run("rfkill", "list")
        if listing:
            blocked = parse_rfkill_list(listing)

    radios: list[RadioInfo] = []
    for hci in hcis:
        entry = hci_entries.get(hci)
        dbus_address, dbus_powered = bluez.get(hci, (None, None))
        address = _read_text(sysfs / hci / "address")
        if address and not _ADDR_RE.match(address.lower()):
            address = None
        address = address or dbus_address or (entry.address if entry is not None else None)
        if address is None:
            continue  # cannot identify this radio; the protocol pins radios by address
        bus = None
        try:
            bus = bus_from_device_path(os.path.realpath(sysfs / hci / "device"))
        except OSError:
            pass
        if bus is None and entry is not None:
            bus = entry.bus
        powered = dbus_powered if dbus_powered is not None else (entry.up if entry is not None else None)
        radios.append(
            RadioInfo(
                address=address.lower(),
                hci=hci,
                bus=bus,
                powered=powered,
                blocked=blocked.get(hci),
            )
        )
    return radios


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None
