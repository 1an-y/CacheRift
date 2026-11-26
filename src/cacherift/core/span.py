"""Lightweight UTF-8 JSON byte-offset scanner preserving wire coordinates."""

from __future__ import annotations

import json
import re

from cacherift.core.models import ByteSpan, OffsetSpace

_WHITESPACE = frozenset(b" \t\r\n")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_BYTES = frozenset(b"-0123456789.eE+")


def _format_child_path(parent: str, key: str) -> str:
    if _IDENT_RE.fullmatch(key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


class _JsonSpanScanner:
    def __init__(self, raw: bytes, space: OffsetSpace) -> None:
        self._raw = raw
        self._len = len(raw)
        self._space = space
        self.spans: dict[str, ByteSpan] = {}

    def scan(self) -> dict[str, ByteSpan]:
        pos = self._skip_ws(0)
        if pos >= self._len:
            raise ValueError("Empty JSON payload")
        end = self._scan_value(pos, "$")
        end = self._skip_ws(end)
        if end != self._len:
            raise ValueError(f"Trailing bytes after JSON root at byte offset {end}")
        return self.spans

    def _skip_ws(self, pos: int) -> int:
        raw = self._raw
        n = self._len
        while pos < n and raw[pos] in _WHITESPACE:
            pos += 1
        return pos

    def _scan_value(self, pos: int, path: str) -> int:
        pos = self._skip_ws(pos)
        if pos >= self._len:
            raise ValueError(f"Unexpected end of JSON at path {path}")
        ch = self._raw[pos]
        if ch == 0x7B:  # '{'
            end = self._scan_object(pos, path)
        elif ch == 0x5B:  # '['
            end = self._scan_array(pos, path)
        elif ch == 0x22:  # '"'
            end = self._scan_string(pos)
        elif ch in _NUMBER_BYTES:
            end = self._scan_number(pos)
        elif self._raw.startswith(b"true", pos):
            end = pos + 4
        elif self._raw.startswith(b"false", pos):
            end = pos + 5
        elif self._raw.startswith(b"null", pos):
            end = pos + 4
        else:
            raise ValueError(f"Unexpected byte 0x{ch:02x} at offset {pos} ({path})")

        self.spans[path] = ByteSpan(space=self._space, start=pos, end=end)
        return end

    def _scan_object(self, pos: int, path: str) -> int:
        pos = self._skip_ws(pos + 1)
        if pos < self._len and self._raw[pos] == 0x7D:  # '}'
            return pos + 1

        while True:
            pos = self._skip_ws(pos)
            if pos >= self._len or self._raw[pos] != 0x22:
                raise ValueError(f"Expected object key string at offset {pos} ({path})")
            key_end = self._scan_string(pos)
            key_text = json.loads(self._raw[pos:key_end].decode("utf-8"))
            if not isinstance(key_text, str):
                raise ValueError(f"Expected string key at offset {pos}")

            pos = self._skip_ws(key_end)
            if pos >= self._len or self._raw[pos] != 0x3A:  # ':'
                raise ValueError(f"Expected ':' after key at offset {pos} ({path})")
            child_path = _format_child_path(path, key_text)
            pos = self._scan_value(pos + 1, child_path)

            pos = self._skip_ws(pos)
            if pos >= self._len:
                raise ValueError(f"Unterminated JSON object at {path}")
            sep = self._raw[pos]
            if sep == 0x7D:  # '}'
                return pos + 1
            if sep != 0x2C:  # ','
                raise ValueError(f"Expected ',' or '}}' at offset {pos} ({path})")
            pos += 1

    def _scan_array(self, pos: int, path: str) -> int:
        pos = self._skip_ws(pos + 1)
        if pos < self._len and self._raw[pos] == 0x5D:  # ']'
            return pos + 1

        idx = 0
        while True:
            child_path = f"{path}[{idx}]"
            pos = self._scan_value(pos, child_path)
            idx += 1

            pos = self._skip_ws(pos)
            if pos >= self._len:
                raise ValueError(f"Unterminated JSON array at {path}")
            sep = self._raw[pos]
            if sep == 0x5D:  # ']'
                return pos + 1
            if sep != 0x2C:  # ','
                raise ValueError(f"Expected ',' or ']' at offset {pos} ({path})")
            pos += 1

    def _scan_string(self, pos: int) -> int:
        raw = self._raw
        n = self._len
        pos += 1  # opening quote
        while pos < n:
            b = raw[pos]
            if b == 0x22:  # '"'
                return pos + 1
            if b == 0x5C:  # '\\'
                pos += 2
                continue
            pos += 1
        raise ValueError("Unterminated JSON string literal")

    def _scan_number(self, pos: int) -> int:
        raw = self._raw
        n = self._len
        while pos < n and raw[pos] in _NUMBER_BYTES:
            pos += 1
        return pos


def scan_json_spans(
    raw: bytes,
    *,
    space: OffsetSpace = OffsetSpace.WIRE_RAW,
) -> dict[str, ByteSpan]:
    """Scan raw UTF-8 JSON bytes and return exact byte spans keyed by JSONPath."""
    return _JsonSpanScanner(raw, space).scan()
