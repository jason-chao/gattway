"""UUID helpers: short forms expand to the Bluetooth base UUID."""

from __future__ import annotations

import re

from .errors import BAD_REQUEST, GattwayError

BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"

_FULL = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHORT = re.compile(r"^(0x)?([0-9a-f]{4}|[0-9a-f]{8})$")


def expand_uuid(value: str) -> str:
    """Return ``value`` as a full lowercase 128-bit UUID string.

    Accepts a full UUID, a 16-bit short form (``"ae3b"``) or a 32-bit short
    form (``"0000ae3b"``), with or without a ``0x`` prefix. Raises
    ``GattwayError(bad_request)`` for anything else.
    """
    if not isinstance(value, str):
        raise GattwayError(BAD_REQUEST, "uuid must be a string")
    v = value.strip().lower()
    if _FULL.match(v):
        return v
    m = _SHORT.match(v)
    if m:
        return m.group(2).rjust(8, "0") + BASE_SUFFIX
    raise GattwayError(BAD_REQUEST, f"not a uuid: {value!r}")


def same_uuid(a: str, b: str) -> bool:
    try:
        return expand_uuid(a) == expand_uuid(b)
    except GattwayError:
        return False
