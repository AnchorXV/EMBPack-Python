"""
embpack_v2.py — EMB Archive Tool for Dragon Ball Xenoverse 2
Rewrite of the original embpack C++ tool with bug fixes and new features.
Compatible with PyInstaller for .exe compilation.

Usage:
  embpack_v2.py [options] <file.emb | folder | glob>

Modes (auto-detected or via flags):
  Extract : input is a .emb file
  Repack  : input is a folder

Options:
  --extract, -x          Force extract mode
  --repack,  -r          Force repack mode
  --list,    -l          List contents of .emb without extracting
  --add      FILE        Add a file to an existing .emb
  --remove   NAME        Remove a file by name from a .emb
  --output,  -o PATH     Set output directory / filename
  --dry-run              Preview actions without writing files
  --verify               After repack, verify binary round-trip integrity
  --silent               Suppress all output except errors
  --verbose              Show detailed per-file information
  --no-wait              Do not pause at end (default for scripting)
  --wait                 Always pause at end
  --version              Show version and exit
"""

# ─── Standard Library ────────────────────────────────────────────────────────
import sys
import os
import struct
import shutil
import hashlib
import argparse
import glob
import copy
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# ─── Version ─────────────────────────────────────────────────────────────────
__version__ = "2.0.1"

# ─── Constants ───────────────────────────────────────────────────────────────
EMB_SIGNATURE       = b"#EMB"
EMB_ENDIAN_LITTLE   = 0xFFFE
EMB_ENDIAN_BIG      = 0xFEFF
EMB_HEADER_SIZE     = 0x20
EMB_DATA_BASE       = 0x20   # == data_table_address; C++ calls this "wrong_data_table_address"
                              # rel_offset formula: file_start - (EMB_DATA_BASE + i*8)
EMB_ALIGN           = 0x40
MAX_FILE_SIZE       = 256 * 1024 * 1024   # 256 MB sanity cap per entry
MAX_FILE_COUNT      = 65535               # sanity cap for file count

# ─── ANSI Colours ────────────────────────────────────────────────────────────
# Windows 10+ supports ANSI in ConHost / Windows Terminal natively.
# PyInstaller builds run in ConHost so we enable it explicitly.

def _enable_win_ansi() -> None:
    """Enable ANSI escape codes on Windows via SetConsoleMode."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        for handle_id in (6, 7):   # stdout=7, stderr=6? actually stdout=CONOUT$
            handle = kernel32.GetStdHandle(-10 - handle_id)
            if handle:
                mode = ctypes.c_ulong()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass

_enable_win_ansi()

class C:
    """ANSI colour helpers."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    RED     = "\033[91m"
    YELLOW  = "\033[93m"
    GREEN   = "\033[92m"
    CYAN    = "\033[96m"
    BLUE    = "\033[94m"
    GREY    = "\033[90m"
    WHITE   = "\033[97m"

    @staticmethod
    def error(msg: str) -> str:
        return f"{C.RED}{C.BOLD}[ERROR]{C.RESET} {C.RED}{msg}{C.RESET}"

    @staticmethod
    def warn(msg: str) -> str:
        return f"{C.YELLOW}{C.BOLD}[WARN]{C.RESET}  {C.YELLOW}{msg}{C.RESET}"

    @staticmethod
    def ok(msg: str) -> str:
        return f"{C.GREEN}{C.BOLD}[OK]{C.RESET}    {C.GREEN}{msg}{C.RESET}"

    @staticmethod
    def info(msg: str) -> str:
        return f"{C.CYAN}[INFO]{C.RESET}  {msg}"

    @staticmethod
    def detail(msg: str) -> str:
        return f"{C.GREY}        {msg}{C.RESET}"

# ─── Logger ──────────────────────────────────────────────────────────────────

class Logger:
    silent  : bool = False
    verbose : bool = False
    _errors : int  = 0
    _warns  : int  = 0

    def error(self, msg: str) -> None:
        self._errors += 1
        print(C.error(msg), file=sys.stderr)

    def warn(self, msg: str) -> None:
        self._warns += 1
        if not self.silent:
            print(C.warn(msg))

    def ok(self, msg: str) -> None:
        if not self.silent:
            print(C.ok(msg))

    def info(self, msg: str) -> None:
        if not self.silent:
            print(C.info(msg))

    def detail(self, msg: str) -> None:
        if self.verbose and not self.silent:
            print(C.detail(msg))

    def dry(self, msg: str) -> None:
        if not self.silent:
            print(f"{C.BLUE}{C.BOLD}[DRY]{C.RESET}   {C.BLUE}{msg}{C.RESET}")

    @property
    def has_errors(self) -> bool:
        return self._errors > 0

    @property
    def has_warns(self) -> bool:
        return self._warns > 0

log = Logger()


# ─── Path Safety ─────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """
    Prevent path traversal attacks from crafted .emb files.
    Strips directory components and illegal Windows filename characters.
    """
    # Remove any directory separators — only keep the bare filename
    name = os.path.basename(name.replace("\\", "/"))
    # Replace characters illegal on Windows filesystems
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    # Collapse leading dots/spaces that hide files on Windows
    name = name.lstrip(". ")
    return name if name else "_unnamed"


def safe_output_path(base_dir: Path, filename: str) -> Path:
    """
    Resolve the final output path and ensure it stays inside base_dir.
    Raises ValueError if the resolved path escapes base_dir.
    """
    clean = sanitize_filename(filename)
    resolved = (base_dir / clean).resolve()
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(
            f"Path traversal blocked: '{filename}' would escape output directory."
        )
    return resolved


# ─── Binary I/O Helpers ──────────────────────────────────────────────────────

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


# ─── EMBFile ─────────────────────────────────────────────────────────────────

class EMBFile:
    """
    Represents a single file entry inside an .emb archive.

    Fixes vs original C++:
    - No malloc/free — data is stored as Python bytes (GC-managed, no leaks).
    - clone() does not invoke file I/O (was a bug in original).
    - Name is sanitized on load to prevent path traversal.
    """

    __slots__ = ("name", "data", "index", "_original_name")

    def __init__(
        self,
        name: str = "",
        data: bytes = b"",
        index: int = 0,
    ):
        self._original_name = name          # kept for XML round-trip fidelity
        self.name  = name
        self.data  = data                   # immutable bytes — no accidental mutation
        self.index = index

    @classmethod
    def from_disk(cls, path: Path) -> "EMBFile":
        """Load a file from disk into an EMBFile entry."""
        data = path.read_bytes()
        return cls(name=path.name, data=data)

    def save_to_disk(self, path: Path, dry_run: bool = False) -> None:
        """Write entry data to disk."""
        if not self.data:
            log.detail(f"Skipping empty entry '{self.name}' (null file).")
            return
        if dry_run:
            log.dry(f"Would write: {path}  ({len(self.data):,} bytes)")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.data)
        log.detail(f"Saved: {path.name}  ({len(self.data):,} bytes)")

    def clone(self) -> "EMBFile":
        """Deep-copy this entry. No file I/O (fixes original C++ bug)."""
        return EMBFile(
            name=self.name,
            data=self.data,   # bytes is immutable, safe to share
            index=self.index,
        )

    def __repr__(self) -> str:
        return (
            f"EMBFile(index={self.index}, name={self.name!r}, "
            f"size={len(self.data):,})"
        )


# ─── EMB ─────────────────────────────────────────────────────────────────────

class EMBLoadError(Exception):
    pass


class EMB:
    """
    Reads and writes Xenoverse 2 .emb archives.

    Bug fixes vs original C++:
    1. isfound never set True  → fixed: use proper set-based duplicate tracking.
    2. malloc without free     → fixed: Python bytes, GC handles memory.
    3. No data_size validation → fixed: sanity-check size before reading.
    4. Path traversal          → fixed: sanitize_filename() on every entry name.
    5. clone() file I/O bug    → fixed: EMBFile.clone() is pure memory copy.
    6. Silent failures         → fixed: all errors raise EMBLoadError or log.error.
    """

    XML_FILENAME = "embFiles.xml"

    def __init__(self) -> None:
        self.name:        str        = ""
        self.version:     str        = "0.0.0.0"
        self.files:       List[EMBFile] = []
        self.big_endian:  bool       = False
        self._had_names:  bool       = False   # mirrors debugHaveNames

    # ── Load ────────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "EMB":
        """
        Parse a .emb file from disk.
        Raises EMBLoadError on any structural problem.
        """
        emb = cls()
        emb.name = path.stem

        raw = path.read_bytes()
        if len(raw) < EMB_HEADER_SIZE:
            raise EMBLoadError(f"File too small to be a valid .emb: {path}")

        # ── Signature ───────────────────────────────────────────────────────
        if raw[:4] != EMB_SIGNATURE:
            raise EMBLoadError(
                f"Bad signature: expected {EMB_SIGNATURE!r}, "
                f"got {raw[:4]!r} in {path.name}"
            )

        # ── Endianness flag ─────────────────────────────────────────────────
        endian_flag = struct.unpack_from("<H", raw, 4)[0]
        if endian_flag == EMB_ENDIAN_BIG:
            emb.big_endian = True
        elif endian_flag == EMB_ENDIAN_LITTLE:
            emb.big_endian = False
        else:
            log.warn(
                f"Unknown endian flag 0x{endian_flag:04X} in {path.name}; "
                f"assuming little-endian."
            )

        r = BinaryReader(raw, big_endian=emb.big_endian)

        # ── Header fields ───────────────────────────────────────────────────
        r.seek(0x06)
        header_size = r.u16()   # usually 0x20, informational only

        ver_parts = [r.u8() for _ in range(4)]
        emb.version = ".".join(str(v) for v in ver_parts)

        file_count = r.u32()

        if file_count > MAX_FILE_COUNT:
            raise EMBLoadError(
                f"Implausible file count {file_count} in {path.name}. "
                f"File may be corrupt."
            )

        # ── Table addresses ─────────────────────────────────────────────────
        r.seek(0x18)
        data_table_addr     = r.u32()
        filename_table_addr = r.u32()

        log.info(
            f"Loading {path.name}  "
            f"[v{emb.version}, {file_count} files, "
            f"{'BE' if emb.big_endian else 'LE'}]"
        )

        # ── Data entries ─────────────────────────────────────────────────────
        for i in range(file_count):
            r.seek(data_table_addr + i * 8)
            rel_offset = r.u32()
            data_size  = r.u32()

            # Sanity checks (fix: original had none)
            if data_size > MAX_FILE_SIZE:
                raise EMBLoadError(
                    f"Entry {i}: data_size={data_size:,} exceeds "
                    f"{MAX_FILE_SIZE // (1024*1024)} MB cap. File may be corrupt."
                )

            abs_offset = EMB_DATA_BASE + i * 8 + rel_offset

            if data_size > 0:
                if abs_offset + data_size > len(raw):
                    raise EMBLoadError(
                        f"Entry {i}: data would read past end of file "
                        f"(offset=0x{abs_offset:X}, size={data_size})."
                    )
                data = raw[abs_offset : abs_offset + data_size]
            else:
                data = b""

            entry = EMBFile(name="", data=data, index=i)
            emb.files.append(entry)
            log.detail(f"  Entry {i:3d}: offset=0x{abs_offset:X}, size={data_size:,}")

        # ── Filename table ───────────────────────────────────────────────────
        emb._had_names = (filename_table_addr != 0)

        if filename_table_addr:
            for i in range(file_count):
                r.seek(filename_table_addr + i * 4)
                str_addr = r.u32()
                if str_addr == 0 or str_addr >= len(raw):
                    continue
                r.seek(str_addr)
                raw_name = r.c_string()
                # Fix: don't sanitize empty strings — "" must stay "" to preserve
                # null-entry round-trip and correct detect_filename_mode() behaviour.
                safe_name = sanitize_filename(raw_name) if raw_name else raw_name
                if safe_name != raw_name:
                    log.warn(
                        f"Entry {i}: name '{raw_name}' sanitized to '{safe_name}'."
                    )
                if i < len(emb.files):
                    emb.files[i].name = safe_name
                    emb.files[i]._original_name = raw_name
                log.detail(f"  Entry {i:3d}: name='{safe_name}'")

        return emb


    # ── Save ────────────────────────────────────────────────────────────────

    def save(
        self,
        path: Path,
        enable_filenames: Optional[bool] = None,
        dry_run: bool = False,
    ) -> bytes:
        """
        Serialize the EMB archive to bytes and optionally write to disk.
        Returns the serialized bytes (useful for --verify).

        enable_filenames=None → auto-detect via detect_filename_mode().
        """
        if enable_filenames is None:
            enable_filenames = self.detect_filename_mode()

        w = BinaryWriter(big_endian=self.big_endian)

        file_total = len(self.files)

        # ─────────────────────────────────────────────────────────────────────
        # Header layout — traced exactly from C++ save():
        #
        #   writeHeader()              → 0x00-0x07  (sig 4B + endian flag 2B + null 2B)
        #   goToAddress(0x6) + writes  → 0x06-0x0B  (header_size u16 + version 4×u8)
        #   writeInt32E(file_count)    → 0x0C-0x0F
        #   writeNull(8)               → 0x10-0x17  (padding)
        #   writeNull(2×sizeof(u32))   → 0x18-0x1F  ← placeholders for the two
        #                                              table addresses, patched later
        #                                              via goToAddress(0x18)
        #   data_table_address = getCurrentAddress() → 0x20  ✅
        #   writeNull(file_total × 8)  → data table starts at 0x20
        # ─────────────────────────────────────────────────────────────────────

        # 0x00 – signature (4 bytes)
        w.write(EMB_SIGNATURE)
        # 0x04 – endian flag (2 bytes)
        w.u16(EMB_ENDIAN_BIG if self.big_endian else EMB_ENDIAN_LITTLE)
        # 0x06 – header_size (2 bytes)
        w.u16(EMB_HEADER_SIZE)
        # 0x08 – version (4 × u8)
        ver_parts = self.version.split(".")
        for i in range(4):
            v = int(ver_parts[i]) if i < len(ver_parts) else 0
            w.u8(v & 0xFF)
        # 0x0C – file_count (u32)
        w.u32(file_total)
        # 0x10 – 8 null bytes (padding)
        w.write_null(8)

        assert w.pos == 0x18, f"Header layout error: expected pos 0x18, got 0x{w.pos:X}"

        # 0x18 – two u32 placeholders (data_table_address + filename_table_address).
        # These are the writeNull(2 * sizeof(uint32_t)) from C++ — patched at the
        # very end via goToAddress(0x18), exactly as the original does.
        data_table_ptr_pos     = w.pos   # 0x18
        w.u32(0)
        filename_table_ptr_pos = w.pos   # 0x1C
        w.u32(0)

        assert w.pos == 0x20, f"Header layout error: expected pos 0x20, got 0x{w.pos:X}"

        # ── Data Table — starts at 0x20, immediately after the header ────────
        data_table_addr = w.pos          # = 0x20  (matches C++ wrong_data_table_address)
        w.u32_at(data_table_ptr_pos, data_table_addr)
        w.write_null(file_total * 8)   # N × (offset u32 + size u32)

        # ── Filename string-pointer table placeholder ─────────────────────────
        filename_table_addr = 0
        if enable_filenames:
            filename_table_addr = w.pos
            w.u32_at(filename_table_ptr_pos, filename_table_addr)
            w.write_null(file_total * 4)   # N × string_ptr u32

        # ── Write file data ───────────────────────────────────────────────────
        for i, entry in enumerate(self.files):
            w.align(EMB_ALIGN)
            file_data_start = w.pos

            if entry.data:
                w.write(entry.data)
                # rel_offset = file_data_start - (EMB_DATA_BASE + i*8)
                rel_offset = file_data_start - (EMB_DATA_BASE + i * 8)
                size       = len(entry.data)
            else:
                # Null entry — original sets both to 0
                rel_offset = 0
                size       = 0

            # Patch data table entry
            w.u32_at(data_table_addr + i * 8,     rel_offset)
            w.u32_at(data_table_addr + i * 8 + 4, size)

        # ── Write filenames ───────────────────────────────────────────────────
        if enable_filenames:
            w.align(EMB_ALIGN)
            for i, entry in enumerate(self.files):
                str_ptr = w.pos
                # Write the *original* name (before sanitize) to preserve round-trip
                name_to_write = entry._original_name or entry.name
                w.c_string(name_to_write)
                w.u32_at(filename_table_addr + i * 4, str_ptr)

        result = w.getvalue()

        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(result)
            log.ok(f"Saved: {path}  ({len(result):,} bytes, {file_total} entries)")
        else:
            log.dry(
                f"Would save: {path}  ({len(result):,} bytes, {file_total} entries)"
            )

        return result


    # ── Extract ──────────────────────────────────────────────────────────────

    def extract(
        self,
        out_dir: Path,
        dry_run: bool = False,
    ) -> None:
        """
        Extract all entries to out_dir and write embFiles.xml.

        Fix: duplicate filename logic now correctly marks isfound=True.
        """
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)

        log.info(
            f"Extracting {len(self.files)} files → {out_dir}"
        )

        # ── Duplicate-name resolution ────────────────────────────────────────
        # FIX: original C++ never set isfound=True, causing every filename to be
        # re-added to the list on every iteration. Correct logic below uses a
        # dict for O(1) lookup and correct counter tracking.
        seen: dict[str, int] = {}   # filename → duplicate count

        xml_root = ET.Element("EMB", version=self.version,
                              debugHaveNames="true" if self._had_names else "false")
        xml_tree = ET.ElementTree(xml_root)

        for i, entry in enumerate(self.files):
            # ── Determine display name ────────────────────────────────────────
            orig_name = entry.name
            if not orig_name:
                orig_name = f"DATA{i:03d}.dds" if i < 1000 else \
                            f"DATA{i:04d}.dds" if i < 10000 else \
                            f"DATA{i:05d}.dds"

            # Filesystem-safe name (strip any path, sanitize)
            filename = sanitize_filename(orig_name)

            # ── Resolve duplicates (FIX: was broken in original) ──────────────
            if filename in seen:
                seen[filename] += 1
                stem = Path(filename).stem
                ext  = Path(filename).suffix
                filename = f"{stem}_{seen[filename]}{ext}"
            else:
                seen[filename] = 0

            # ── Save entry ────────────────────────────────────────────────────
            out_path = out_dir / filename
            entry.save_to_disk(out_path, dry_run=dry_run)

            # ── XML record ────────────────────────────────────────────────────
            ET.SubElement(
                xml_root, "File",
                name=orig_name,
                filename=filename,
                index=str(i),
                size=str(len(entry.data)),
            )

        # ── Write XML ─────────────────────────────────────────────────────────
        xml_path = out_dir / self.XML_FILENAME
        if not dry_run:
            _write_pretty_xml(xml_tree, xml_path)
            log.ok(f"Wrote manifest: {xml_path.name}")
        else:
            log.dry(f"Would write manifest: {xml_path}")

    # ── Add Folder ───────────────────────────────────────────────────────────

    def add_folder(self, folder: Path) -> None:
        """
        Scan a folder and populate self.files.
        Respects embFiles.xml for original ordering and names.
        """
        if not folder.is_dir():
            raise EMBLoadError(f"Input folder does not exist: {folder}")

        xml_path = folder / self.XML_FILENAME

        # Collect all files except the XML manifest
        disk_files: List[EMBFile] = []
        for p in sorted(folder.iterdir()):
            if p.is_file() and p.name != self.XML_FILENAME:
                disk_files.append(EMBFile.from_disk(p))
                log.detail(f"  Found: {p.name}  ({p.stat().st_size:,} bytes)")

        self.files = disk_files

        # ── Reorder and rename according to XML ──────────────────────────────
        if xml_path.exists():
            try:
                self._apply_xml_order(xml_path)
            except Exception as e:
                log.warn(f"Could not apply XML ordering ({e}); using filesystem order.")

        # ── Restore null-file names ───────────────────────────────────────────
        # Entries that are zero-size and were auto-named "DATA???.dds" should
        # have their name cleared so the round-trip is accurate.
        for entry in self.files:
            if (
                len(entry.data) == 0
                and entry.name.startswith("DATA")
                and entry.name.endswith(".dds")
            ):
                entry.name = ""

        log.info(
            f"Loaded {len(self.files)} files from {folder.name}/"
        )

    def _apply_xml_order(self, xml_path: Path) -> None:
        """Parse embFiles.xml and reorder self.files to match recorded order."""
        tree   = ET.parse(xml_path)
        root   = tree.getroot()

        ver = root.get("version")
        if ver:
            self.version = ver

        ordered_filenames: List[str] = []
        ordered_names:     List[str] = []

        for node in root.findall("File"):
            fname = node.get("filename") or node.get("name", "")
            name  = node.get("name", fname)
            ordered_filenames.append(fname)
            ordered_names.append(name)

        if not ordered_filenames:
            return

        # Build lookup: disk filename → EMBFile
        by_name: dict[str, EMBFile] = {e.name: e for e in self.files}

        ordered: List[EMBFile] = []
        for filename, orig_name in zip(ordered_filenames, ordered_names):
            if filename in by_name:
                entry = by_name.pop(filename)
                # Restore original (possibly different) name for the archive
                entry._original_name = orig_name
                if filename != orig_name:
                    entry.name = orig_name
                ordered.append(entry)

        # Append any new files not present in the XML (added after extract)
        for leftover in by_name.values():
            ordered.append(leftover)

        self.files = ordered


    # ── Utilities ────────────────────────────────────────────────────────────

    def detect_filename_mode(self) -> bool:
        """
        Returns True if the archive should store filenames.
        False if all entries are auto-named DATA???.dds (texture-only packs).
        """
        for entry in self.files:
            name = entry.name or entry._original_name
            if not (name.startswith("DATA") and name.endswith(".dds")):
                return True
        return False

    def list_entries(self) -> None:
        """Print a formatted table of entries to stdout."""
        if not self.files:
            log.info("Archive is empty.")
            return

        total_bytes = sum(len(e.data) for e in self.files)
        header = (
            f"\n{C.BOLD}{C.WHITE}"
            f"{'#':>4}  {'Name':<40}  {'Size':>12}  {'SHA-1':>10}"
            f"{C.RESET}"
        )
        print(header)
        print(C.GREY + "─" * 74 + C.RESET)

        for entry in self.files:
            sha = hashlib.sha1(entry.data).hexdigest()[:8] if entry.data else "─" * 8
            name_display = entry.name or f"(unnamed/{entry.index})"
            size_display = f"{len(entry.data):,}" if entry.data else "0 (null)"
            print(
                f"{entry.index:>4}  "
                f"{C.CYAN}{name_display:<40}{C.RESET}  "
                f"{size_display:>12}  "
                f"{C.GREY}{sha}{C.RESET}"
            )

        print(C.GREY + "─" * 74 + C.RESET)
        print(
            f"{C.BOLD}Total: {len(self.files)} entries, "
            f"{total_bytes:,} bytes uncompressed{C.RESET}\n"
        )

    def add_entry(self, file_path: Path) -> None:
        """Append a single file as a new entry (--add)."""
        if not file_path.exists():
            raise FileNotFoundError(f"--add: file not found: {file_path}")
        entry = EMBFile.from_disk(file_path)
        entry.index = len(self.files)
        self.files.append(entry)
        log.ok(f"Added entry: {entry.name}  ({len(entry.data):,} bytes)")

    def remove_entry(self, name: str) -> bool:
        """
        Remove an entry by name (case-insensitive). Returns True if found.
        """
        name_lower = name.lower()
        for i, entry in enumerate(self.files):
            if entry.name.lower() == name_lower:
                removed = self.files.pop(i)
                # Re-index remaining entries
                for j in range(i, len(self.files)):
                    self.files[j].index = j
                log.ok(f"Removed entry: {removed.name}")
                return True
        log.warn(f"--remove: no entry named '{name}' found.")
        return False

    def clone(self) -> "EMB":
        """Deep copy of this EMB archive."""
        emb = EMB()
        emb.name       = self.name
        emb.version    = self.version
        emb.big_endian = self.big_endian
        emb._had_names = self._had_names
        emb.files      = [e.clone() for e in self.files]
        return emb

    def verify_against(self, original_path: Path) -> bool:
        """
        Re-load the just-saved file and compare SHA-1 hashes of each entry
        against self. Structural comparison, not raw byte comparison
        (alignment padding may differ slightly).
        Returns True if all entries match.
        """
        try:
            reloaded = EMB.load(original_path)
        except EMBLoadError as e:
            log.error(f"Verify: could not reload saved file: {e}")
            return False

        if len(reloaded.files) != len(self.files):
            log.error(
                f"Verify: entry count mismatch "
                f"(expected {len(self.files)}, got {len(reloaded.files)})."
            )
            return False

        all_ok = True
        for orig, new in zip(self.files, reloaded.files):
            h_orig = hashlib.sha1(orig.data).hexdigest()
            h_new  = hashlib.sha1(new.data).hexdigest()
            if h_orig != h_new:
                log.error(
                    f"Verify: entry {orig.index} '{orig.name}' data mismatch.\n"
                    f"         original SHA-1: {h_orig}\n"
                    f"         saved    SHA-1: {h_new}"
                )
                all_ok = False
            else:
                log.detail(f"  Verify OK: {orig.name}  ({h_orig[:8]})")

        return all_ok


# ─── XML Helper ──────────────────────────────────────────────────────────────

def _write_pretty_xml(tree: ET.ElementTree, path: Path) -> None:
    """Write an ElementTree to disk with indented formatting."""
    raw = ET.tostring(tree.getroot(), encoding="unicode")
    reparsed = minidom.parseString(raw)
    pretty = reparsed.toprettyxml(indent="  ", encoding="utf-8")
    path.write_bytes(pretty)


# ─── Argument Parser ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="embpack_v2",
        description=(
            f"{C.BOLD}{C.CYAN}embpack_v2{C.RESET} — "
            "EMB Archive Tool for Dragon Ball Xenoverse 2\n"
            "Extract, repack, inspect, and modify .emb archives.\n\n"
            "Auto-detects mode: .emb file → extract, folder → repack.\n"
            "Supports glob patterns for batch processing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  embpack_v2 shader.emb                   # extract\n"
            "  embpack_v2 shader/                       # repack\n"
            "  embpack_v2 -l shader.emb                 # list contents\n"
            "  embpack_v2 -x shader.emb -o out/         # extract to custom dir\n"
            "  embpack_v2 --add new.dds shader.emb      # add file to archive\n"
            "  embpack_v2 --remove old.dds shader.emb   # remove file\n"
            "  embpack_v2 --verify shader/              # repack + verify\n"
            "  embpack_v2 *.emb                         # batch extract\n"
        ),
    )

    p.add_argument(
        "inputs",
        nargs="+",
        metavar="FILE|FOLDER",
        help="Input .emb file(s), folder(s), or glob pattern(s).",
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "-x", "--extract",
        action="store_true",
        help="Force extract mode (default when input is .emb).",
    )
    mode.add_argument(
        "-r", "--repack",
        action="store_true",
        help="Force repack mode (default when input is a folder).",
    )
    mode.add_argument(
        "-l", "--list",
        action="store_true",
        help="List contents of .emb file(s) without extracting.",
    )

    p.add_argument(
        "--add",
        metavar="FILE",
        default=None,
        help="Add FILE to the .emb archive (use with a .emb input).",
    )
    p.add_argument(
        "--remove",
        metavar="NAME",
        default=None,
        help="Remove entry by name from the .emb archive.",
    )
    p.add_argument(
        "-o", "--output",
        metavar="PATH",
        default=None,
        help="Output directory (extract) or filename (repack).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview all actions without writing any files.",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="After repack, reload the .emb and verify data integrity.",
    )
    p.add_argument(
        "--silent",
        action="store_true",
        help="Suppress all output except errors.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-file detail during processing.",
    )
    p.add_argument(
        "--wait",
        action="store_true",
        help="Pause and wait for Enter at the end (useful for drag-and-drop).",
    )
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="Never pause at the end (default for scripting).",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"embpack_v2 {__version__}",
    )
    return p


# ─── Per-Input Processing ────────────────────────────────────────────────────

def process_extract(
    emb_path: Path,
    out_dir: Optional[Path],
    dry_run: bool,
) -> bool:
    """Load and extract a single .emb file. Returns True on success."""
    try:
        emb = EMB.load(emb_path)
    except (EMBLoadError, Exception) as e:
        log.error(f"Failed to load '{emb_path.name}': {e}")
        return False

    dest = out_dir if out_dir else emb_path.parent / emb_path.stem
    emb.extract(dest, dry_run=dry_run)
    return True


def process_repack(
    folder: Path,
    out_path: Optional[Path],
    dry_run: bool,
    verify: bool,
) -> bool:
    """Repack a folder into a .emb file. Returns True on success."""
    emb = EMB()
    try:
        emb.add_folder(folder)
    except (EMBLoadError, Exception) as e:
        log.error(f"Failed to read folder '{folder}': {e}")
        return False

    dest = out_path if out_path else folder.parent / (folder.name + ".emb")
    # Strip trailing slash/dot from folder name if needed
    if dest.suffix.lower() != ".emb":
        dest = dest.with_suffix(".emb")

    emb.save(dest, dry_run=dry_run)

    if verify and not dry_run:
        log.info("Running integrity verification…")
        ok = emb.verify_against(dest)
        if ok:
            log.ok("Verification passed — all entries match.")
        else:
            log.error("Verification FAILED — see errors above.")
            return False

    return True


def process_list(emb_path: Path) -> bool:
    """Print a listing of entries inside a .emb file."""
    try:
        emb = EMB.load(emb_path)
    except (EMBLoadError, Exception) as e:
        log.error(f"Failed to load '{emb_path.name}': {e}")
        return False

    print(
        f"\n{C.BOLD}{C.WHITE}{emb_path.name}{C.RESET}  "
        f"{C.GREY}[v{emb.version}, {'BE' if emb.big_endian else 'LE'}]{C.RESET}"
    )
    emb.list_entries()
    return True


def process_add(
    emb_path: Path,
    add_file: Path,
    out_path: Optional[Path],
    dry_run: bool,
) -> bool:
    """Add a file to an existing .emb archive."""
    try:
        emb = EMB.load(emb_path)
    except (EMBLoadError, Exception) as e:
        log.error(f"Failed to load '{emb_path.name}': {e}")
        return False

    try:
        emb.add_entry(add_file)
    except FileNotFoundError as e:
        log.error(str(e))
        return False

    dest = out_path or emb_path
    emb.save(dest, dry_run=dry_run)
    return True


def process_remove(
    emb_path: Path,
    remove_name: str,
    out_path: Optional[Path],
    dry_run: bool,
) -> bool:
    """Remove a named entry from a .emb archive."""
    try:
        emb = EMB.load(emb_path)
    except (EMBLoadError, Exception) as e:
        log.error(f"Failed to load '{emb_path.name}': {e}")
        return False

    if not emb.remove_entry(remove_name):
        return False

    dest = out_path or emb_path
    emb.save(dest, dry_run=dry_run)
    return True


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = build_parser()

    # Show help if no arguments at all (e.g. double-click .exe)
    if len(sys.argv) == 1:
        parser.print_help()
        _pause(force=True)
        return 0

    args = parser.parse_args()

    # ── Logger setup ─────────────────────────────────────────────────────────
    log.silent  = args.silent
    log.verbose = args.verbose

    if args.dry_run:
        log.info(
            f"{C.BLUE}{C.BOLD}DRY RUN MODE{C.RESET} — no files will be written."
        )

    # ── Expand inputs (glob + direct paths) ──────────────────────────────────
    resolved: List[Path] = []
    for pattern in args.inputs:
        matches = [Path(p) for p in glob.glob(pattern, recursive=True)]
        if matches:
            resolved.extend(matches)
        else:
            # Could be a literal path that exists
            p = Path(pattern)
            if p.exists():
                resolved.append(p)
            else:
                log.warn(f"No match for input: '{pattern}'")

    if not resolved:
        log.error("No valid input files or folders found.")
        _pause(args)
        return 1

    # ── Batch processing loop ─────────────────────────────────────────────────
    success_count = 0
    fail_count    = 0
    out_path      = Path(args.output) if args.output else None
    add_file      = Path(args.add) if args.add else None

    for inp in resolved:
        is_emb    = inp.is_file() and inp.suffix.lower() == ".emb"
        is_folder = inp.is_dir()

        # Determine effective mode
        if args.list:
            mode = "list"
        elif args.add or args.remove:
            mode = "edit"
        elif args.extract:
            mode = "extract"
        elif args.repack:
            mode = "repack"
        elif is_emb:
            mode = "extract"
        elif is_folder:
            mode = "repack"
        else:
            log.error(
                f"Cannot determine mode for '{inp}'. "
                f"Expected a .emb file or a folder."
            )
            fail_count += 1
            continue

        # ── Dispatch ──────────────────────────────────────────────────────────
        ok = False

        if mode == "list":
            if not is_emb:
                log.error(f"--list requires a .emb file, got: '{inp}'")
                fail_count += 1
                continue
            ok = process_list(inp)

        elif mode == "extract":
            if not is_emb:
                log.error(f"Extract mode requires a .emb file, got: '{inp}'")
                fail_count += 1
                continue
            # For batch, each file gets its own subdirectory unless -o is set
            # and there is only one input.
            dest = out_path if (out_path and len(resolved) == 1) else None
            ok = process_extract(inp, dest, args.dry_run)

        elif mode == "repack":
            if not is_folder:
                log.error(f"Repack mode requires a folder, got: '{inp}'")
                fail_count += 1
                continue
            dest = out_path if (out_path and len(resolved) == 1) else None
            ok = process_repack(inp, dest, args.dry_run, args.verify)

        elif mode == "edit":
            if not is_emb:
                log.error(f"--add/--remove requires a .emb file, got: '{inp}'")
                fail_count += 1
                continue
            if args.add:
                ok = process_add(inp, add_file, out_path, args.dry_run)
            elif args.remove:
                ok = process_remove(inp, args.remove, out_path, args.dry_run)

        if ok:
            success_count += 1
        else:
            fail_count += 1

    # ── Summary ───────────────────────────────────────────────────────────────
    total = success_count + fail_count
    if total > 1:
        log.info(
            f"Done: {C.GREEN}{success_count} succeeded{C.RESET}, "
            f"{C.RED}{fail_count} failed{C.RESET}  "
            f"({total} total)"
        )
    else:
        if not log.has_errors:
            log.ok("Finished.")
        else:
            log.info("Finished with errors.")

    _pause(args)
    return 0 if fail_count == 0 else 1


def _pause(args=None, force: bool = False) -> None:
    """Conditionally pause and wait for Enter."""
    if force:
        print("\nPress Enter to continue…")
        input()
        return

    if args is None:
        return

    # --wait always pauses; --no-wait never pauses
    # default (neither flag): pause only when there are errors (like original)
    if getattr(args, "wait", False):
        print("\nPress Enter to continue…")
        input()
    elif not getattr(args, "no_wait", False):
        if log.has_errors:
            print("\nPress Enter to continue…")
            input()


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(main())