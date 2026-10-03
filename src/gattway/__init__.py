"""gattway: lend a host's Bluetooth Low Energy radios over a WebSocket."""

__version__ = "0.1.1"

PROTOCOL = "gattway"
PROTOCOL_VERSION = 1

from .errors import GattwayError  # noqa: E402

__all__ = ["GattwayError", "PROTOCOL", "PROTOCOL_VERSION", "__version__"]
