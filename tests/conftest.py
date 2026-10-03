import asyncio
import logging

import pytest

from gattway.client import Gattway
from gattway.config import Config, ServerConfig
from gattway.radios.fake import FakeBackend
from gattway.server import GattwayServer

logging.getLogger("websockets").setLevel(logging.WARNING)


def make_config(**server_kwargs) -> Config:
    server_kwargs.setdefault("status_interval_s", 0.5)
    server = ServerConfig(host="127.0.0.1", port=0, tick_s=0.05, **server_kwargs)
    return Config(name="test-instance", server=server)


class Running:
    def __init__(self, server: GattwayServer, backend: FakeBackend):
        self.server = server
        self.backend = backend

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}/ws"

    @property
    def device(self):
        return self.backend.device()

    def client(self, name: str = "tester", **kwargs) -> Gattway:
        return Gattway(self.url, name=name, **kwargs)


async def start_server(config: Config | None = None) -> Running:
    backend = FakeBackend()
    backend.radio.scan_sleep = False  # scans return at once; real timing is tested elsewhere
    server = GattwayServer(config or make_config(), backend)
    await server.start()
    return Running(server, backend)


@pytest.fixture
async def running():
    r = await start_server()
    try:
        yield r
    finally:
        await r.server.stop()


@pytest.fixture
async def g(running):
    client = running.client()
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


async def wait_for(predicate, timeout=3.0, step=0.01):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()
