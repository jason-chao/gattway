# gattway

gattway lends a host's Bluetooth Low Energy radios to programs on the network.
It runs on the machine that has the radios (a Raspberry Pi, say) and exposes
them over one WebSocket. A client scans, connects to a device, writes to and
reads from its characteristics, and receives its notifications. gattway knows
nothing about what the devices are.

- Device-agnostic: UUIDs and bytes in, UUIDs and bytes out.
- Several radios per host, pinned by BD address, each holding several devices.
- One owner per device. Everyone can see the device in `status`; only the owner
  can talk to it. There is no takeover.
- One safety behaviour: **farewell writes**. The owner registers the writes that
  put its device in a safe state; if the owner's socket closes or its
  heartbeat stops, gattway performs them, then disconnects the device.
- **Timed writes**: queue frames at absolute instance times so network jitter
  does not reach the device.

The protocol is small and documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Quick start

Python 3.11 or newer.

```sh
git clone https://github.com/jason-chao/gattway.git
cd gattway
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

gattway --fake                                   # serves a scripted fake device
gattway-cli ws://127.0.0.1:7120/ws scan          # in another shell
gattway-cli ws://127.0.0.1:7120/ws watch 00:fa:ke:00:00:01 fa03
```

The fake device `fake-echo` echoes every write to `fa01` back as a notification
on `fa02` and counts at 10 Hz on `fa03`. Without `--fake`, gattway uses the
real radios on the host through BlueZ.

`gattway-cli` commands: `status`, `radios`, `scan [--timeout N] [--prefix NAME]`,
`connect ADDR`, `read ADDR CHAR`, `write ADDR CHAR HEX`, `watch ADDR CHAR`.

## Configuration

gattway reads a TOML file from `$GATTWAY_CONFIG`, else
`/etc/gattway/gattway.toml`, else `./gattway.toml`. Everything is optional.
See [gattway.example.toml](gattway.example.toml).

```toml
[instance]
name = "lab-pi"           # default: hostname

[server]
host = "0.0.0.0"
port = 7120
token = ""                # clients pass ?token=... when set

[[radio]]
label = "usb"
address = "00:11:22:33:44:55"
enabled = true
default = true

[fake]
enabled = false
```

Environment overrides: `GATTWAY_NAME`, `GATTWAY_HOST`, `GATTWAY_PORT`,
`GATTWAY_TOKEN`, `GATTWAY_FAKE`.

Radio rules (protocol section 7): a configured radio that is absent is listed
with `present: false` and requests for it fail with `no_radio`; gattway never
substitutes another radio. With no radios configured, every radio present is
enabled and the first is the default. If radios are configured and none is
marked `default`, the first enabled one is.

## Deploying on a Raspberry Pi

```sh
sudo deploy/install.sh
```

This creates a venv in `/opt/gattway`, installs a systemd unit that runs as
your user, powers on only the enabled radios before start, and enables it.
Details, radio pinning and troubleshooting: [deploy/README.md](deploy/README.md).

## The Python client in ten lines

```python
import asyncio
from gattway.client import Gattway

async def main():
    async with Gattway("ws://127.0.0.1:7120/ws", name="demo") as g:
        dev = await g.connect_device("00:fa:ke:00:00:01")
        await dev.farewell([("fa01", b"\x00")])          # written if we vanish
        await dev.subscribe("fa02", lambda data, t: print(data.hex()))
        await dev.write("fa01", b"\x01\x02")
        t0 = dev.instance_time() + 0.1                   # instance clock
        await dev.write_at("fa01", [(t0 + i * 0.1, bytes([i])) for i in range(3)])
        await asyncio.sleep(1)
        await dev.disconnect()                           # no farewell on request

asyncio.run(main())
```

`Gattway` heartbeats every 2 s and estimates the clock offset from the echoes;
`dev.instance_time()` converts local time for `write_at`. Replies with
`ok: false` raise `GattwayError(code, message, data)`. The client never
reconnects on its own: when the socket closes, `g.closed` is set and
`g.on_closed` fires, and the application decides what to do.

Callbacks: `g.on_status`, `g.on_notify`, `g.on_disconnected`, `g.on_closed`,
`dev.on_disconnected`.

## Embedding

Another program can run a gattway instance around its own radios. Implement
`gattway.radios.Radio` (scan, connect) and `gattway.radios.DeviceLink` (read,
write, subscribe, unsubscribe, disconnect), wrap them in a `RadioBackend`, and
pass a factory:

```python
from gattway.config import load_config
from gattway.server import serve

async def my_backend(config):
    return MyBackend()          # a gattway.radios.RadioBackend

asyncio.run(serve(load_config(), radio_factory=my_backend))
```

`gattway.radios.FakeBackend` is a complete small example. For finer control,
`gattway.server.GattwayServer(config, backend)` has `start()`, `stop()` and
`port`; the test suite runs it that way.

## Development

```sh
pip install -e '.[dev]'
pytest -q
```

The tests start the real server in-process with the fake radio and drive it
through the Python client.

## Licence

MIT. Copyright 2026 Jason Chao.
