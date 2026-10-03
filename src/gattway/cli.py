"""Console scripts: ``gattway`` (server) and ``gattway-cli`` (client)."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
from typing import Any

from . import __version__
from .client import Device, Gattway
from .config import ConfigError, load_config
from .errors import GattwayError


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ----------------------------------------------------------------------------
# gattway (server)


def main_server(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gattway", description="Lend this host's BLE radios over a WebSocket.")
    parser.add_argument("--config", metavar="PATH", help="TOML config file (default: $GATTWAY_CONFIG, /etc/gattway/gattway.toml, ./gattway.toml)")
    parser.add_argument("--fake", action="store_true", help="serve a scripted fake device instead of real radios")
    parser.add_argument("--host", help="override [server] host")
    parser.add_argument("--port", type=int, help="override [server] port")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"gattway {__version__}")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"gattway: {exc}", file=sys.stderr)
        return 2
    if args.fake:
        config.fake = True
    if args.host:
        config.server.host = args.host
    if args.port is not None:
        config.server.port = args.port

    from .server import serve

    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
    return 0


# ----------------------------------------------------------------------------
# gattway-cli (client)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=False))


def _stamp(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]


async def _cli(args: argparse.Namespace) -> int:
    g = Gattway(args.url, name=args.name, token=args.token, timeout_s=args.request_timeout)
    try:
        await g.connect()
    except GattwayError as exc:
        print(f"gattway-cli: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"gattway-cli: cannot connect to {args.url}: {exc}", file=sys.stderr)
        return 1
    try:
        return await _run_command(g, args)
    except GattwayError as exc:
        print(f"gattway-cli: {exc}" + (f" {json.dumps(exc.data)}" if exc.data else ""), file=sys.stderr)
        return 1
    finally:
        await g.close()


async def _run_command(g: Gattway, args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "status":
        # The first status frame follows hello almost at once; wait briefly for it.
        for _ in range(50):
            if g.status is not None:
                break
            await asyncio.sleep(0.02)
        _print(g.status or g.hello)
        return 0
    if cmd == "radios":
        _print(await g.radios())
        return 0
    if cmd == "scan":
        devices = await g.scan(timeout_s=args.timeout, name_prefix=args.prefix, radio=args.radio)
        for d in devices:
            rssi = f"{d['rssi']:>4}" if d.get("rssi") is not None else "   ?"
            print(f"{d['address']}  {rssi} dBm  {d.get('name') or ''}")
        if not devices:
            print("no devices found", file=sys.stderr)
        return 0

    dev: Device = await g.connect_device(args.address, radio=args.radio)
    try:
        if cmd == "connect":
            _print({"address": dev.address, "name": dev.name, "radio": dev.radio, "services": dev.services})
            return 0
        if cmd == "read":
            data = await dev.read(args.char)
            print(data.hex())
            return 0
        if cmd == "write":
            await dev.write(args.char, args.hex, response=args.response)
            return 0
        if cmd == "watch":
            stop = asyncio.Event()

            def on_notify(data: bytes, t: float) -> None:
                print(f"{_stamp(t)}  {data.hex()}", flush=True)

            dev.on_disconnected = lambda msg: (print(f"disconnected: {msg.get('reason')}", file=sys.stderr), stop.set())
            g.on_closed = stop.set
            await dev.subscribe(args.char, on_notify)
            print(f"watching {dev.address} {args.char}; Ctrl-C to stop", file=sys.stderr)
            try:
                await stop.wait()
            except asyncio.CancelledError:
                pass
            return 0
        return 2
    finally:
        if dev.connected and g.connected:
            try:
                await dev.disconnect()
            except GattwayError:
                pass


def main_client(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gattway-cli", description="Talk to a gattway instance.")
    parser.add_argument("url", help="instance URL, e.g. ws://127.0.0.1:7120/ws")
    parser.add_argument("--name", default="gattway-cli", help="client name shown to others")
    parser.add_argument("--token", help="instance token (or put ?token= in the URL)")
    parser.add_argument("--radio", help="radio label or address (default: the instance default)")
    parser.add_argument("--request-timeout", type=float, default=10.0, help=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print the instance status")
    sub.add_parser("radios", help="list radios")
    p = sub.add_parser("scan", help="scan for devices")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds (1 to 30)")
    p.add_argument("--prefix", help="only names starting with this")
    p = sub.add_parser("connect", help="connect, print services, disconnect")
    p.add_argument("address")
    p = sub.add_parser("read", help="read a characteristic")
    p.add_argument("address")
    p.add_argument("char")
    p = sub.add_parser("write", help="write hex bytes to a characteristic")
    p.add_argument("address")
    p.add_argument("char")
    p.add_argument("hex")
    p.add_argument("--response", action="store_true", help="write with response")
    p = sub.add_parser("watch", help="subscribe and print notifications until Ctrl-C")
    p.add_argument("address")
    p.add_argument("char")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return asyncio.run(_cli(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main_server())
