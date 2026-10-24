"""Allow-listed, typed JSON codec for persisting frozen domain aggregates.

Domain aggregates are frozen ``dataclass(slots=True)`` trees plus small
``__slots__`` value objects (ids, codes). This codec encodes them as tagged
JSON and decodes them back **only** into classes defined under
``amrp.domain`` (plus ``datetime``/``date``/``Decimal``/
``UUID``/``tuple``/``frozenset``). It never unpickles, imports arbitrary
modules, or calls arbitrary callables, so a tampered database file cannot
execute code. Dataclasses are rebuilt through their constructors, so domain
``__post_init__`` invariants are re-validated on every load.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

_ALLOWED_PREFIX = "amrp.domain"
# Rows written before the package rename (financial_intelligence -> amrp) still decode.
_LEGACY_PREFIX = "financial_intelligence.domain"
_TYPE = "__t"


class CodecError(ValueError):
    """Raised when a payload cannot be encoded or safely decoded."""


def _qualname(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _slot_names(cls: type) -> list[str]:
    names: list[str] = []
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        names.extend(s for s in slots if s not in ("__weakref__", "__dict__") and s not in names)
    return names


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        if not type(value).__module__.startswith(_ALLOWED_PREFIX):
            raise CodecError(f"refusing to encode non-domain enum {_qualname(type(value))}")
        return {_TYPE: "enum", "c": _qualname(type(value)), "v": value.value}
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return {_TYPE: "datetime", "v": value.isoformat()}
    if isinstance(value, date):
        return {_TYPE: "date", "v": value.isoformat()}
    if isinstance(value, Decimal):
        return {_TYPE: "decimal", "v": str(value)}
    if isinstance(value, UUID):
        return {_TYPE: "uuid", "v": str(value)}
    if isinstance(value, tuple):
        return {_TYPE: "tuple", "v": [_encode(item) for item in value]}
    if isinstance(value, frozenset):
        return {_TYPE: "frozenset", "v": sorted((_encode(i) for i in value), key=json.dumps)}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {_TYPE: "dict", "v": [[_encode(k), _encode(v)] for k, v in value.items()]}
    cls = type(value)
    if not cls.__module__.startswith(_ALLOWED_PREFIX):
        raise CodecError(f"refusing to encode non-domain type {_qualname(cls)}")
    if dataclasses.is_dataclass(value):
        fields = {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {_TYPE: "dc", "c": _qualname(cls), "f": fields}
    slots = _slot_names(cls)
    if not slots:
        raise CodecError(f"cannot encode domain type without slots: {_qualname(cls)}")
    return {
        _TYPE: "obj",
        "c": _qualname(cls),
        "f": {name: _encode(getattr(value, name)) for name in slots},
    }


def _resolve(name: str) -> type:
    module_name, _, qualname = name.partition(":")
    if module_name.startswith(_LEGACY_PREFIX):
        module_name = "amrp.domain" + module_name[len(_LEGACY_PREFIX) :]
    if not module_name.startswith(_ALLOWED_PREFIX) or not qualname:
        raise CodecError(f"refusing to decode type outside the domain: {name}")
    module = importlib.import_module(module_name)
    target: Any = module
    for part in qualname.split("."):
        target = getattr(target, part, None)
        if target is None:
            raise CodecError(f"unknown domain type: {name}")
    if not isinstance(target, type) or not target.__module__.startswith(_ALLOWED_PREFIX):
        raise CodecError(f"not a domain class: {name}")
    return target


def _decode(data: Any) -> Any:
    if data is None or isinstance(data, bool | int | float | str):
        return data
    if isinstance(data, list):
        return [_decode(item) for item in data]
    if not isinstance(data, dict) or _TYPE not in data:
        raise CodecError("untagged object in payload")
    tag = data[_TYPE]
    if tag == "datetime":
        return datetime.fromisoformat(data["v"])
    if tag == "date":
        return date.fromisoformat(data["v"])
    if tag == "decimal":
        return Decimal(data["v"])
    if tag == "uuid":
        return UUID(data["v"])
    if tag == "tuple":
        return tuple(_decode(item) for item in data["v"])
    if tag == "frozenset":
        return frozenset(_decode(item) for item in data["v"])
    if tag == "dict":
        return {_decode(k): _decode(v) for k, v in data["v"]}
    cls = _resolve(data["c"])
    if tag == "enum":
        if not issubclass(cls, Enum):
            raise CodecError("enum tag on non-enum class")
        return cls(data["v"])
    fields = {name: _decode(value) for name, value in data["f"].items()}
    if tag == "dc":
        if not dataclasses.is_dataclass(cls):
            raise CodecError("dataclass tag on non-dataclass")
        init = {f.name for f in dataclasses.fields(cls) if f.init}
        instance = cls(**{k: v for k, v in fields.items() if k in init})
        for key, value in fields.items():
            if key not in init:
                object.__setattr__(instance, key, value)
        return instance
    if tag == "obj":
        if dataclasses.is_dataclass(cls) or set(fields) != set(_slot_names(cls)):
            raise CodecError("slot object shape mismatch")
        instance = object.__new__(cls)
        for key, value in fields.items():
            object.__setattr__(instance, key, value)
        return instance
    raise CodecError(f"unknown tag {tag!r}")


def dumps(value: Any) -> str:
    """Encode a domain aggregate as compact, deterministic JSON text."""
    return json.dumps(_encode(value), separators=(",", ":"), sort_keys=True)


def loads(text: str) -> Any:
    """Decode JSON text produced by :func:`dumps` (allow-listed types only)."""
    try:
        return _decode(json.loads(text))
    except CodecError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CodecError(f"invalid persisted payload: {type(exc).__name__}") from None
