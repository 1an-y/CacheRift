"""Incremental Server-Sent Events parser that never holds back bytes from the caller."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# Frames after which a streamed model response is over; any usage the provider sends is final.
_TERMINAL_TYPES = frozenset(
    {"response.completed", "response.incomplete", "response.failed", "message_stop"}
)
_CHAT_DONE_SENTINEL = "[DONE]"
_ERROR_TYPES = frozenset({"error"})


class SSEAccumulator:
    """Feed raw response bytes in any chunking and get parsed events out.

    The caller keeps forwarding the same bytes to its client; this class only keeps the tail of
    a line that has not completed yet. Events are dicts: a JSON object payload is kept as parsed,
    a non-JSON payload is kept verbatim under ``data``.
    """

    def __init__(self, *, require_terminal_frame: bool = True) -> None:
        """Pass ``require_terminal_frame=False`` for streams that end without announcing it."""
        self._require_terminal_frame = require_terminal_frame
        self._buffer = bytearray()
        self._event_name: str | None = None
        self._data_lines: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.terminal_seen = False
        self.error_seen = False

    @property
    def complete(self) -> bool:
        """True when no error frame was seen and the stream reached its proper end."""
        if self.error_seen:
            return False
        return self.terminal_seen or not self._require_terminal_frame

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while (line := self._take_line()) is not None:
            self._on_line(line)

    def finish(self) -> None:
        """End of stream.

        A trailing CR is a complete line terminator (nothing follows to complete a CRLF). A line
        that was never terminated, and any event whose blank line never came, is dropped.
        """
        if self._buffer.endswith(b"\r"):
            self._buffer += b"\n"
            while (line := self._take_line()) is not None:
                self._on_line(line)
        self._buffer.clear()
        self._event_name = None
        self._data_lines.clear()

    def _take_line(self) -> bytes | None:
        """Pop the next complete line (LF, CRLF or CR terminated) from the buffer."""
        buffer = self._buffer
        cr = buffer.find(b"\r")
        lf = buffer.find(b"\n")
        if cr == -1 and lf == -1:
            return None
        if cr != -1 and (lf == -1 or cr < lf):
            if cr == len(buffer) - 1:
                return None  # may be the first half of a CRLF pair: wait for the next byte
            line = bytes(buffer[:cr])
            del buffer[: cr + (2 if buffer[cr + 1] == 0x0A else 1)]
            return line
        line = bytes(buffer[:lf])
        del buffer[: lf + 1]
        return line

    def _on_line(self, line: bytes) -> None:
        if not line:
            self._dispatch()
            return
        if line.startswith(b":"):
            return  # comment / keep-alive
        name, _, value = line.partition(b":")
        if value.startswith(b" "):
            value = value[1:]
        text = value.decode("utf-8", errors="replace")
        if name == b"data":
            self._data_lines.append(text)
        elif name == b"event":
            self._event_name = text

    def _dispatch(self) -> None:
        if not self._data_lines:
            self._event_name = None
            return  # a frame without data dispatches nothing
        event_name, self._event_name = self._event_name, None
        data = "\n".join(self._data_lines)
        self._data_lines.clear()
        self._append(_parse_event(event_name, data))

    def _append(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        event_type = event.get("type")
        if event_type in _TERMINAL_TYPES or event.get("data") == _CHAT_DONE_SENTINEL:
            self.terminal_seen = True
        # Chat Completions reports trouble as a frame holding an `error` object and no `type`.
        if event_type in _ERROR_TYPES or isinstance(event.get("error"), Mapping):
            self.error_seen = True


def _parse_event(event_name: str | None, data: str) -> dict[str, Any]:
    try:
        payload: Any = json.loads(data)
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        event = dict(payload)
        if not isinstance(event.get("type"), str) and event_name is not None:
            event["type"] = event_name
        return event
    return {"type": event_name, "data": data} if event_name is not None else {"data": data}
