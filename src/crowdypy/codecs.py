"""State codecs: how a store turns state bytes into values and back.

The World Stores keep every payload as bytes and decode through a codec only when asked, so
a malformed payload counts as a decode failure instead of breaking ingest. :func:`json_codec`,
:data:`raw_codec` and :data:`text_codec` cover the common cases. :func:`struct_codec`
describes a fixed binary layout (CrowdyJS's ``structCodec`` DSL) and can decode a whole
column of actor states in one numpy call (:meth:`StructCodec.decode_many`).
"""

from __future__ import annotations

import importlib
import json
import struct
from collections.abc import Mapping
from typing import Any, Protocol, TypeVar

__all__ = [
    "StateCodec",
    "StructCodec",
    "bool8",
    "f32",
    "f64",
    "fixed_bytes",
    "i8",
    "i16",
    "i32",
    "json_codec",
    "raw_codec",
    "reserved",
    "struct_codec",
    "text_codec",
    "u8",
    "u16",
    "u32",
]

T = TypeVar("T")


class StateCodec(Protocol[T]):
    def encode(self, value: T) -> bytes: ...

    def decode(self, data: bytes) -> T: ...


class _Json:
    def encode(self, value: Any) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    def decode(self, data: bytes) -> Any:
        return json.loads(bytes(data).decode("utf-8"))


class _Raw:
    def encode(self, value: bytes) -> bytes:
        return bytes(value)

    def decode(self, data: bytes) -> bytes:
        return bytes(data)


class _Text:
    def encode(self, value: str) -> bytes:
        return value.encode("utf-8")

    def decode(self, data: bytes) -> str:
        return bytes(data).decode("utf-8")


def json_codec() -> StateCodec[Any]:
    """JSON in UTF-8 (the stores' default for durable state)."""
    return _Json()


#: The bytes as they are.
raw_codec: StateCodec[bytes] = _Raw()
#: UTF-8 text.
text_codec: StateCodec[str] = _Text()


class _Field:
    __slots__ = ("dtype", "fmt", "skip")

    def __init__(self, fmt: str, dtype: str, *, skip: bool = False) -> None:
        self.fmt = fmt
        self.dtype = dtype
        self.skip = skip


def f32() -> _Field:
    return _Field("f", "f4")


def f64() -> _Field:
    return _Field("d", "f8")


def u8() -> _Field:
    return _Field("B", "u1")


def u16() -> _Field:
    return _Field("H", "u2")


def u32() -> _Field:
    return _Field("I", "u4")


def i8() -> _Field:
    return _Field("b", "i1")


def i16() -> _Field:
    return _Field("h", "i2")


def i32() -> _Field:
    return _Field("i", "i4")


def bool8() -> _Field:
    return _Field("?", "?")


def fixed_bytes(length: int) -> _Field:
    return _Field(f"{length}s", f"V{length}")


def reserved(length: int) -> _Field:
    """Padding: written as zeros, left out of the decoded value."""
    return _Field(f"{length}x", f"V{length}", skip=True)


class StructCodec:
    """A fixed-size binary layout, field by field, in declaration order."""

    def __init__(self, spec: Mapping[str, _Field], *, little_endian: bool = True) -> None:
        self.fields = dict(spec)
        order = "<" if little_endian else ">"
        self._struct = struct.Struct(order + "".join(f.fmt for f in self.fields.values()))
        self._names = [name for name, f in self.fields.items() if not f.skip]
        self._little_endian = little_endian

    @property
    def size(self) -> int:
        """Bytes per encoded value."""
        return self._struct.size

    def encode(self, value: Mapping[str, Any]) -> bytes:
        return self._struct.pack(*(value[name] for name in self._names))

    def decode(self, data: bytes) -> dict[str, Any]:
        return dict(zip(self._names, self._struct.unpack_from(data), strict=True))

    @property
    def dtype(self) -> Any:
        """The numpy structured dtype of one value (padding included, unnamed)."""
        numpy = importlib.import_module("numpy")
        order = "<" if self._little_endian else ">"
        names: list[str] = []
        formats: list[str] = []
        for index, (name, field) in enumerate(self.fields.items()):
            names.append(f"_pad{index}" if field.skip else name)
            kind = field.dtype
            formats.append(kind if kind.startswith(("V", "?", "u1", "i1")) else order + kind)
        return numpy.dtype({"names": names, "formats": formats})

    def decode_many(self, offsets: Any, data: Any) -> Any:
        """Decode a column of states (``offsets`` delimiting ``data``, as an actor snapshot
        gives them) into one numpy structured array, without a Python loop. Every state must
        be exactly :attr:`size` bytes; others raise ``ValueError``."""
        numpy = importlib.import_module("numpy")
        lengths = numpy.diff(numpy.asarray(offsets))
        if lengths.size and not numpy.all(lengths == self.size):
            raise ValueError(f"every state must be {self.size} bytes for this codec")
        return numpy.frombuffer(memoryview(data), dtype=self.dtype, count=int(lengths.size))


def struct_codec(spec: Mapping[str, _Field], *, little_endian: bool = True) -> StructCodec:
    """A codec for a fixed binary layout::

    pose = struct_codec({"x": f32(), "y": f32(), "z": f32(), "yaw": u16(), "_": reserved(2)})
    """
    return StructCodec(spec, little_endian=little_endian)
