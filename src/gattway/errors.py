"""Protocol error codes and the exception that carries them."""

from __future__ import annotations

from typing import Any

# Error codes defined in docs/PROTOCOL.md section 2.
BAD_REQUEST = "bad_request"
UNKNOWN_OP = "unknown_op"
NO_RADIO = "no_radio"
NOT_FOUND = "not_found"
CONNECT_FAILED = "connect_failed"
HELD = "held"
NOT_OWNER = "not_owner"
NOT_CONNECTED = "not_connected"
NO_CHARACTERISTIC = "no_characteristic"
WRITE_FAILED = "write_failed"
READ_FAILED = "read_failed"
TIMEOUT = "timeout"
BUSY = "busy"
UNAUTHORISED = "unauthorised"

# Used by the client library only: the socket closed before a reply arrived.
CLOSED = "closed"

ERROR_CODES = frozenset(
    {
        BAD_REQUEST,
        UNKNOWN_OP,
        NO_RADIO,
        NOT_FOUND,
        CONNECT_FAILED,
        HELD,
        NOT_OWNER,
        NOT_CONNECTED,
        NO_CHARACTERISTIC,
        WRITE_FAILED,
        READ_FAILED,
        TIMEOUT,
        BUSY,
        UNAUTHORISED,
    }
)


class GattwayError(Exception):
    """A protocol error.

    Raised by radios and the server to produce an error reply, and by the
    client library when a reply carries ``ok: false``.
    """

    def __init__(self, code: str, message: str = "", data: dict[str, Any] | None = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.data = data

    def to_json(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
