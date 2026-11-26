"""Deep immutability, order-preserving canonical projection, and structural equality."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, overload


@overload
def freeze_json(value: Mapping[str, Any]) -> MappingProxyType[str, Any]: ...


@overload
def freeze_json(value: list[Any]) -> tuple[Any, ...]: ...


@overload
def freeze_json(value: Any) -> Any: ...


def freeze_json(value: Any) -> Any:
    """Recursively convert mutable mappings and sequences into immutable equivalents.

    Dict insertion order is preserved. A shallow copy is made before wrapping in
    MappingProxyType so subsequent mutations to the caller's dict cannot leak.
    """
    if isinstance(value, Mapping):
        copied = {k: freeze_json(v) for k, v in value.items()}
        return MappingProxyType(copied)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Recursively convert frozen structures back into plain Python dicts and lists."""
    if isinstance(value, Mapping):
        return {k: thaw_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


def canonical_project_bytes(value: Any, *, sort_keys: bool = False) -> bytes:
    """Serialize a JSON-compatible structure into compact UTF-8 bytes.

    Key insertion order is preserved unless ``sort_keys=True`` is explicitly requested.
    """
    plain = thaw_json(value)
    text = json.dumps(
        plain,
        sort_keys=sort_keys,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def order_sensitive_equal(a: Any, b: Any) -> bool:
    """Compare two JSON values while preserving key order and distinguishing bool/int/float."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        if len(a) != len(b):
            return False
        for key_a, key_b in zip(a.keys(), b.keys(), strict=True):
            if type(key_a) is not type(key_b) or key_a != key_b:
                return False
            if not order_sensitive_equal(a[key_a], b[key_b]):
                return False
        return True

    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(
            order_sensitive_equal(item_a, item_b) for item_a, item_b in zip(a, b, strict=True)
        )

    return type(a) is type(b) and bool(a == b)
