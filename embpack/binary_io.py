# binary_io.py — BinaryReader and BinaryWriter helpers

import struct


class BinaryReader:
    """Thin wrapper around bytes for positional binary reading."""

    def __init__(self, data: bytes, big_endian: bool = False):
        self._data = data
        self._pos  = 0
        self.big_endian = big_endian

    @property
    def pos(self) -> int:
        return self._pos

    def seek(self, pos: int) -> None:
        self._pos = pos

    def _endian(self) -> str:
        return ">" if self.big_endian else "<"

    def read(self, n: int) -> bytes:
        chunk = self._data[self._pos : self._pos + n]
        if len(chunk) < n:
            raise EOFError(
                f"Attempted to read {n} bytes at 0x{self._pos:X} "
                f"but only {len(chunk)} available."
            )
        self._pos += n
        return chunk

    def u8(self)  -> int: return struct.unpack("B",  self.read(1))[0]
    def u16(self) -> int: return struct.unpack(self._endian() + "H", self.read(2))[0]
    def u32(self) -> int: return struct.unpack(self._endian() + "I", self.read(4))[0]

    def c_string(self) -> str:
        """Read a null-terminated UTF-8 string."""
        buf = bytearray()
        while True:
            b = self._data[self._pos : self._pos + 1]
            self._pos += 1
            if not b or b == b"\x00":
                break
            buf.extend(b)
        return buf.decode("utf-8", errors="replace")


class BinaryWriter:
    """Builds a bytearray in memory for positional binary writing."""

    def __init__(self, big_endian: bool = False):
        self._buf: bytearray = bytearray()
        self.big_endian = big_endian

    def _endian(self) -> str:
        return ">" if self.big_endian else "<"

    @property
    def pos(self) -> int:
        return len(self._buf)

    def seek_write(self, pos: int, data: bytes) -> None:
        """Overwrite bytes at an already-written position."""
        if pos + len(data) > len(self._buf):
            raise IndexError(
                f"seek_write: pos 0x{pos:X} + {len(data)} exceeds buffer size."
            )
        self._buf[pos : pos + len(data)] = data

    def write(self, data: bytes) -> None:
        self._buf.extend(data)

    def write_null(self, n: int) -> None:
        self._buf.extend(b"\x00" * n)

    def u8(self, v: int) -> None:
        self._buf.extend(struct.pack("B", v))

    def u16(self, v: int) -> None:
        self._buf.extend(struct.pack(self._endian() + "H", v))

    def u32(self, v: int) -> None:
        self._buf.extend(struct.pack(self._endian() + "I", v))

    def u32_at(self, pos: int, v: int) -> None:
        """Patch a uint32 at an already-written offset."""
        self.seek_write(pos, struct.pack(self._endian() + "I", v))

    def align(self, multiple: int) -> None:
        """Pad buffer with zeros until length is a multiple of `multiple`."""
        remainder = self.pos % multiple
        if remainder:
            self.write_null(multiple - remainder)

    def c_string(self, s: str) -> None:
        self._buf.extend(s.encode("utf-8") + b"\x00")

    def getvalue(self) -> bytes:
        return bytes(self._buf)
