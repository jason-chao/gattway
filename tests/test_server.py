"""The real server in-process, driven by the Python client over a socket."""

import asyncio
import time

import pytest

from gattway.client import Gattway
from gattway.config import RadioConfig
from gattway.errors import GattwayError
from gattway.radios.fake import (
    FAKE_COUNTER_CHAR,
    FAKE_DEVICE_ADDRESS,
    FAKE_ECHO_CHAR,
    FAKE_RADIO_ADDRESS,
    FAKE_WRITE_CHAR,
)

from conftest import make_config, start_server, wait_for

ADDR = FAKE_DEVICE_ADDRESS


async def test_hello_and_status_shape(running, g):
    hello = g.hello
    assert hello["type"] == "hello"
    assert hello["protocol"] == "gattway" and hello["version"] == 1
    assert hello["instance"]["name"] == "test-instance"
    assert hello["instance"]["software"].startswith("gattway ")
    assert isinstance(hello["t"], float)
    assert hello["devices"] == []
    radio = hello["radios"][0]
    assert set(radio) == {"label", "address", "hci", "bus", "enabled", "default", "present", "powered", "blocked", "scanning"}
    assert radio["address"] == FAKE_RADIO_ADDRESS and radio["enabled"] and radio["default"] and radio["present"]

    assert await wait_for(lambda: g.status is not None)
    status = g.status
    assert set(status) == {"type", "t", "instance", "radios", "devices", "clients"}
    assert [c["name"] for c in status["clients"]] == ["tester"]
    assert abs(g.offset) < 0.5


async def test_scan_with_prefix(running, g):
    found = await g.scan(timeout_s=1)
    assert [d["address"] for d in found] == [ADDR]
    d = found[0]
    assert d["name"] == "fake-echo" and d["radio"] == FAKE_RADIO_ADDRESS
    assert set(d) == {"address", "name", "rssi", "radio", "services", "manufacturer"}
    assert await g.scan(timeout_s=1, name_prefix="fake") == found
    assert await g.scan(timeout_s=1, name_prefix="other") == []
    assert await g.scan(timeout_s=1, services=["fa00"]) == found
    assert await g.scan(timeout_s=1, services=["180f"]) == []
    with pytest.raises(GattwayError) as info:
        await g.scan(timeout_s=0.1)
    assert info.value.code == "bad_request"
    with pytest.raises(GattwayError) as info:
        await g.scan(timeout_s=1, radio="nope")
    assert info.value.code == "no_radio"


async def test_scan_sets_scanning_flag(running, g):
    running.backend.radio.scan_sleep = True
    task = asyncio.ensure_future(g.scan(timeout_s=1))
    assert await wait_for(lambda: g.status and g.status["radios"][0]["scanning"])
    with pytest.raises(GattwayError) as info:
        await g.scan(timeout_s=1)
    assert info.value.code == "busy"
    await task
    assert await wait_for(lambda: not g.status["radios"][0]["scanning"])


async def test_connect_owner_held_not_owner(running, g):
    dev = await g.connect_device(ADDR)
    assert dev.address == ADDR and dev.name == "fake-echo"
    uuids = {c["uuid"] for c in dev.characteristics()}
    assert {FAKE_WRITE_CHAR, FAKE_ECHO_CHAR, FAKE_COUNTER_CHAR} <= uuids
    assert await wait_for(lambda: g.status and g.status["devices"])
    entry = g.status["devices"][0]
    assert entry["owner"] == "tester" and entry["address"] == ADDR
    assert entry["queue"] == {"pending": 0, "written": 0, "late": 0}
    assert entry["farewell"] == 0

    # connecting again as the owner is idempotent
    again = await g.connect_device(ADDR)
    assert again is dev

    async with running.client("intruder") as other:
        with pytest.raises(GattwayError) as info:
            await other.connect_device(ADDR)
        assert info.value.code == "held"
        assert info.value.data == {"owner": "tester"}
        with pytest.raises(GattwayError) as info:
            await other.request("write", address=ADDR, char=FAKE_WRITE_CHAR, data="01")
        assert info.value.code == "not_owner"
        assert info.value.data == {"owner": "tester"}
        for op in ("read", "subscribe", "unsubscribe", "cancel", "disconnect"):
            with pytest.raises(GattwayError) as info:
                await other.request(op, address=ADDR, char=FAKE_WRITE_CHAR)
            assert info.value.code == "not_owner", op
        with pytest.raises(GattwayError) as info:
            await other.request("farewell", address=ADDR, writes=[])
        assert info.value.code == "not_owner"
        with pytest.raises(GattwayError) as info:
            await other.request("read", address="00:11:22:33:44:55", char=FAKE_WRITE_CHAR)
        assert info.value.code == "not_connected"


async def test_errors(running, g):
    with pytest.raises(GattwayError) as info:
        await g.request("nonsense")
    assert info.value.code == "unknown_op"
    with pytest.raises(GattwayError) as info:
        await g.request("connect")
    assert info.value.code == "bad_request"
    with pytest.raises(GattwayError) as info:
        await g.connect_device("00:11:22:33:44:55", timeout_s=1)
    assert info.value.code == "not_found"
    dev = await g.connect_device(ADDR)
    with pytest.raises(GattwayError) as info:
        await dev.write("ffff", b"\x00")
    assert info.value.code == "no_characteristic"
    with pytest.raises(GattwayError) as info:
        await dev.read(FAKE_ECHO_CHAR)
    assert info.value.code == "read_failed"
    with pytest.raises(GattwayError) as info:
        await dev.write(FAKE_ECHO_CHAR, b"\x00")
    assert info.value.code == "write_failed"


async def test_write_read_echo_round_trip(running, g):
    dev = await g.connect_device(ADDR)
    got: list[tuple[bytes, float]] = []
    await dev.subscribe("fa02", lambda data, t: got.append((data, t)))
    assert await wait_for(lambda: g.status and g.status["devices"][0]["subscribed"] == [FAKE_ECHO_CHAR])
    await dev.write("fa01", b"\x01\x02\x03")
    await dev.write("fa01", "a1b2", response=True)
    assert await wait_for(lambda: len(got) == 2)
    assert [d for d, _ in got] == [b"\x01\x02\x03", bytes.fromhex("a1b2")]
    assert all(abs(t - time.time()) < 1.0 for _, t in got)
    assert await dev.read("fa01") == bytes.fromhex("a1b2")
    writes = running.device.writes
    assert [w.response for w in writes] == [False, True]
    await dev.unsubscribe("fa02")
    await dev.write("fa01", b"\x09")
    await asyncio.sleep(0.05)
    assert len(got) == 2


async def test_counter_notifications_at_10hz(running, g):
    dev = await g.connect_device(ADDR)
    got: list[bytes] = []
    await dev.subscribe(FAKE_COUNTER_CHAR, lambda data, t: got.append(data))
    await asyncio.sleep(1.0)
    await dev.unsubscribe(FAKE_COUNTER_CHAR)
    n = len(got)
    assert 8 <= n <= 12, n
    assert [int.from_bytes(b, "big") for b in got] == list(range(n))
    await asyncio.sleep(0.25)
    assert len(got) == n  # stopped after unsubscribe


async def test_write_at_times_replaces_and_drops_late(running, g):
    dev = await g.connect_device(ADDR)
    base = dev.instance_time() + 0.2
    frames = [(base, b"\x01"), (base + 0.1, b"\x02"), (base + 0.2, b"\x03"), (base - 5.0, b"\xee")]
    result = await dev.write_at("fa01", frames)
    assert result == {"queued": 3, "replaced": 0, "late": 1}
    # the same t replaces the pending frame
    result = await dev.write_at("fa01", [(base + 0.1, b"\x22")])
    assert result == {"queued": 1, "replaced": 1, "late": 0}
    status = await g.request("radios")  # any request; status arrives separately
    assert status
    await asyncio.sleep(0.6)
    writes = running.device.writes
    assert [w.data for w in writes] == [b"\x01", b"\x22", b"\x03"]
    expected = [base, base + 0.1, base + 0.2]
    for w, t in zip(writes, expected):
        assert abs(w.t - g.offset - t) < 0.03, (w.t, t)
    assert await wait_for(lambda: g.status["devices"][0]["queue"] == {"pending": 0, "written": 3, "late": 1}, timeout=1.5)


async def test_write_at_cancel(running, g):
    dev = await g.connect_device(ADDR)
    base = dev.instance_time() + 0.3
    await dev.write_at("fa01", [(base + i * 0.05, bytes([i])) for i in range(4)])
    assert await dev.cancel("fa01") == 4
    assert await dev.cancel() == 0
    await asyncio.sleep(0.6)
    assert running.device.writes == []


async def test_farewell_runs_when_owner_stops_heartbeating():
    running = await start_server(make_config(hb_timeout_s=0.6, status_interval_s=0.2))
    try:
        quiet = running.client("quiet", heartbeat=False)
        await quiet.connect()
        dev = await quiet.connect_device(ADDR)
        await dev.farewell([("fa01", b"\x00\x00"), {"char": "fa01", "data": "ff", "response": True}])
        assert await wait_for(lambda: quiet.status and quiet.status["devices"][0]["farewell"] == 2)
        await quiet.heartbeat()  # one beat, then silence
        t_last = time.time()

        async with running.client("watcher", heartbeat_interval_s=0.1) as watcher:
            assert await wait_for(lambda: watcher.status and watcher.status["devices"] == [], timeout=3.0)
            t_gone = time.time()
        assert 0.5 <= t_gone - t_last <= 1.5
        writes = running.device.writes
        assert [(w.data, w.response) for w in writes] == [(b"\x00\x00", False), (b"\xff", True)]
        assert 0.015 <= writes[1].t - writes[0].t <= 0.2
        assert not running.device.connected
        assert await wait_for(quiet.closed.is_set)
        assert quiet.close_code == 4000
    finally:
        await running.server.stop()


async def test_farewell_runs_when_socket_closes(running):
    client = running.client("leaver")
    await client.connect()
    dev = await client.connect_device(ADDR)
    await dev.farewell([("fa01", b"\x00")])
    await client.close()
    assert await wait_for(lambda: not running.device.connected)
    assert [w.data for w in running.device.writes] == [b"\x00"]


async def test_disconnect_does_not_run_farewell(running, g):
    dev = await g.connect_device(ADDR)
    await dev.farewell([("fa01", b"\x00")])
    events = []
    g.on_disconnected = events.append
    await dev.disconnect()
    assert not dev.connected and dev.disconnect_reason == "requested"
    assert await wait_for(lambda: len(events) == 1)
    assert events[0]["reason"] == "requested" and events[0]["requested"] is True
    assert running.device.writes == []
    assert not running.device.connected
    assert await wait_for(lambda: g.status["devices"] == [])
    # and the device is free for someone else
    async with running.client("next") as other:
        dev2 = await other.connect_device(ADDR)
        assert dev2.connected


async def test_peer_drop_reports_to_owner(running, g):
    dev = await g.connect_device(ADDR)
    running.device.drop()
    await asyncio.wait_for(dev.disconnected.wait(), 2.0)
    assert dev.disconnect_reason == "peer"
    assert await wait_for(lambda: g.status["devices"] == [])


async def test_token_required():
    running = await start_server(make_config(token="s3cret"))
    try:
        bad = running.client("nobody")
        with pytest.raises(GattwayError) as info:
            await bad.connect()
        assert info.value.code == "unauthorised"
        wrong = running.client("nobody", token="wrong")
        with pytest.raises(GattwayError) as info:
            await wrong.connect()
        assert info.value.code == "unauthorised"
        async with running.client("somebody", token="s3cret") as ok:
            assert ok.hello["protocol"] == "gattway"
            assert await ok.scan(timeout_s=1)
    finally:
        await running.server.stop()


async def test_configured_but_absent_radio_is_no_radio():
    cfg = make_config()
    cfg.radios = [
        RadioConfig(address="00:11:22:33:44:55", label="usb", enabled=True, default=True),
        RadioConfig(address=FAKE_RADIO_ADDRESS, label="fake", enabled=False),
    ]
    running = await start_server(cfg)
    try:
        async with running.client() as g:
            radios = {r["label"]: r for r in g.hello["radios"]}
            assert radios["usb"]["present"] is False and radios["usb"]["default"] is True
            assert radios["fake"]["present"] is True and radios["fake"]["enabled"] is False
            for which in (None, "usb", "fake", "00:11:22:33:44:55"):
                with pytest.raises(GattwayError) as info:
                    await g.scan(timeout_s=1, radio=which)
                assert info.value.code == "no_radio", which
            with pytest.raises(GattwayError) as info:
                await g.connect_device(ADDR)
            assert info.value.code == "no_radio"
    finally:
        await running.server.stop()


async def test_configured_present_radio_by_label_and_address():
    cfg = make_config()
    cfg.radios = [RadioConfig(address=FAKE_RADIO_ADDRESS, label="fake", enabled=True, default=True)]
    running = await start_server(cfg)
    try:
        async with running.client() as g:
            assert (await g.scan(timeout_s=1, radio="fake"))[0]["radio"] == "fake"
            assert (await g.scan(timeout_s=1, radio=FAKE_RADIO_ADDRESS))[0]["radio"] == "fake"
            dev = await g.connect_device(ADDR)
            assert dev.radio == "fake"
    finally:
        await running.server.stop()


async def test_server_stop_runs_farewell(running):
    g = running.client("owner")
    await g.connect()
    dev = await g.connect_device(ADDR)
    await dev.farewell([("fa01", b"\xaa")])
    await running.server.stop()
    assert [w.data for w in running.device.writes] == [b"\xaa"]
    assert not running.device.connected
    assert await wait_for(g.closed.is_set)
    await g.close()


async def test_heartbeat_offset_estimation(running):
    async with running.client("hb", heartbeat_interval_s=0.1) as g:
        await asyncio.sleep(0.35)
        assert g.rtt is not None and g.rtt < 0.1
        assert abs(g.offset) < 0.02
        assert abs(g.instance_time() - time.time()) < 0.02


async def test_client_does_not_reconnect(running):
    g = running.client("fragile")
    await g.connect()
    closed = []
    g.on_closed = lambda: closed.append(True)
    await running.server.stop()
    assert await wait_for(g.closed.is_set)
    assert closed == [True]
    with pytest.raises(GattwayError) as info:
        await g.scan(timeout_s=1)
    assert info.value.code == "closed"
