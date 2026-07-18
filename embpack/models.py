# models.py — EMBFile, EMBLoadError, and EMB core logic

import hashlib
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional

from embpack.constants import (
    EMB_SIGNATURE, EMB_ENDIAN_LITTLE, EMB_ENDIAN_BIG,
    EMB_HEADER_SIZE, EMB_DATA_BASE, EMB_ALIGN,
    MAX_FILE_SIZE, MAX_FILE_COUNT,
)
from embpack.binary_io import BinaryReader, BinaryWriter
from embpack.pathutils import sanitize_filename
from embpack.logger import log
from embpack.colors import C
from embpack.xml_manifest import write_pretty_xml, read_xml_order


# ─── Exception ───────────────────────────────────────────────────────────────

class EMBLoadError(Exception):
    pass


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
        self._original_name = name      # kept for XML round-trip fidelity
        self.name  = name
        self.data  = data               # immutable bytes — no accidental mutation
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
            data=self.data,     # bytes is immutable, safe to share
            index=self.index,
        )

    def __repr__(self) -> str:
        return (
            f"EMBFile(index={self.index}, name={self.name!r}, "
            f"size={len(self.data):,})"
        )


# ─── EMB ─────────────────────────────────────────────────────────────────────

class EMB:
    """
    Reads and writes Xenoverse 2 .emb archives.

    Bug fixes vs original C++:
    1. isfound never set True  → fixed: use proper dict-based duplicate tracking.
    2. malloc without free     → fixed: Python bytes, GC handles memory.
    3. No data_size validation → fixed: sanity-check size before reading.
    4. Path traversal          → fixed: sanitize_filename() on every entry name.
    5. clone() file I/O bug    → fixed: EMBFile.clone() is pure memory copy.
    6. Silent failures         → fixed: all errors raise EMBLoadError or log.error.
    """

    XML_FILENAME = "embFiles.xml"

    def __init__(self) -> None:
        self.name:       str           = ""
        self.version:    str           = "0.0.0.0"
        self.files:      List[EMBFile] = []
        self.big_endian: bool          = False
        self._had_names: bool          = False  # mirrors debugHaveNames

    # ── Load ─────────────────────────────────────────────────────────────────

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

        # Signature check
        if raw[:4] != EMB_SIGNATURE:
            raise EMBLoadError(
                f"Bad signature: expected {EMB_SIGNATURE!r}, "
                f"got {raw[:4]!r} in {path.name}"
            )

        # Endianness flag
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

        # Header fields
        r.seek(0x06)
        r.u16()              # header_size: skip, unused (usually 0x20)
        ver_parts    = [r.u8() for _ in range(4)]
        emb.version  = ".".join(str(v) for v in ver_parts)
        file_count   = r.u32()

        if file_count > MAX_FILE_COUNT:
            raise EMBLoadError(
                f"Implausible file count {file_count} in {path.name}. "
                f"File may be corrupt."
            )

        # Table addresses
        r.seek(0x18)
        data_table_addr     = r.u32()
        filename_table_addr = r.u32()

        log.info(
            f"Loading {path.name}  "
            f"[v{emb.version}, {file_count} files, "
            f"{'BE' if emb.big_endian else 'LE'}]"
        )

        # Data entries
        for i in range(file_count):
            r.seek(data_table_addr + i * 8)
            rel_offset = r.u32()
            data_size  = r.u32()

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

        # Filename table
        emb._had_names = (filename_table_addr != 0)

        if filename_table_addr:
            for i in range(file_count):
                r.seek(filename_table_addr + i * 4)
                str_addr = r.u32()
                if str_addr == 0 or str_addr >= len(raw):
                    continue
                r.seek(str_addr)
                raw_name = r.c_string()
                # Don't sanitize empty strings — "" must stay "" to preserve
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

    # ── Save ─────────────────────────────────────────────────────────────────

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

        # ── Header ───────────────────────────────────────────────────────────
        # Layout traced exactly from C++ save():
        #   0x00  signature (4B)
        #   0x04  endian flag (2B)
        #   0x06  header_size (2B)
        #   0x08  version (4 × u8)
        #   0x0C  file_count (u32)
        #   0x10  padding (8B)
        #   0x18  data_table_address placeholder (u32)  ← patched later
        #   0x1C  filename_table_address placeholder (u32) ← patched later
        #   0x20  data table begins

        w.write(EMB_SIGNATURE)
        w.u16(EMB_ENDIAN_BIG if self.big_endian else EMB_ENDIAN_LITTLE)
        w.u16(EMB_HEADER_SIZE)

        ver_parts = self.version.split(".")
        for i in range(4):
            v = int(ver_parts[i]) if i < len(ver_parts) else 0
            w.u8(v & 0xFF)

        w.u32(file_total)
        w.write_null(8)     # 0x10–0x17 padding

        assert w.pos == 0x18, f"Header layout error: expected 0x18, got 0x{w.pos:X}"

        data_table_ptr_pos     = w.pos  # 0x18
        w.u32(0)
        filename_table_ptr_pos = w.pos  # 0x1C
        w.u32(0)

        assert w.pos == 0x20, f"Header layout error: expected 0x20, got 0x{w.pos:X}"

        # ── Data table (starts at 0x20) ───────────────────────────────────────
        data_table_addr = w.pos
        w.u32_at(data_table_ptr_pos, data_table_addr)
        w.write_null(file_total * 8)    # N × (rel_offset u32 + size u32)

        # ── Filename pointer table placeholder ────────────────────────────────
        filename_table_addr = 0
        if enable_filenames:
            filename_table_addr = w.pos
            w.u32_at(filename_table_ptr_pos, filename_table_addr)
            w.write_null(file_total * 4)    # N × string_ptr u32

        # ── File data ─────────────────────────────────────────────────────────
        for i, entry in enumerate(self.files):
            w.align(EMB_ALIGN)
            file_data_start = w.pos

            if entry.data:
                w.write(entry.data)
                rel_offset = file_data_start - (EMB_DATA_BASE + i * 8)
                size       = len(entry.data)
            else:
                rel_offset = 0
                size       = 0

            w.u32_at(data_table_addr + i * 8,     rel_offset)
            w.u32_at(data_table_addr + i * 8 + 4, size)

        # ── Filename strings ──────────────────────────────────────────────────
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

    # ── Extract ───────────────────────────────────────────────────────────────

    def extract(self, out_dir: Path, dry_run: bool = False) -> None:
        """
        Extract all entries to out_dir and write embFiles.xml.

        Fix: duplicate filename logic now correctly marks isfound=True.
        """
        if not dry_run:
            out_dir.mkdir(parents=True, exist_ok=True)

        log.info(f"Extracting {len(self.files)} files → {out_dir}")

        # Duplicate-name resolution.
        # FIX: original C++ never set isfound=True, causing every filename to be
        # re-added to the list on every iteration. Correct logic uses a dict for
        # O(1) lookup and correct counter tracking.
        seen: dict[str, int] = {}

        xml_root = ET.Element(
            "EMB",
            version=self.version,
            debugHaveNames="true" if self._had_names else "false",
        )
        xml_tree = ET.ElementTree(xml_root)

        for i, entry in enumerate(self.files):
            orig_name = entry.name
            if not orig_name:
                if i < 1000:
                    orig_name = f"DATA{i:03d}.dds"
                elif i < 10000:
                    orig_name = f"DATA{i:04d}.dds"
                else:
                    orig_name = f"DATA{i:05d}.dds"

            filename = sanitize_filename(orig_name)

            if filename in seen:
                seen[filename] += 1
                stem = Path(filename).stem
                ext  = Path(filename).suffix
                filename = f"{stem}_{seen[filename]}{ext}"
            else:
                seen[filename] = 0

            out_path = out_dir / filename
            entry.save_to_disk(out_path, dry_run=dry_run)

            ET.SubElement(
                xml_root, "File",
                name=orig_name,
                filename=filename,
                index=str(i),
                size=str(len(entry.data)),
            )

        xml_path = out_dir / self.XML_FILENAME
        if not dry_run:
            write_pretty_xml(xml_tree, xml_path)
            log.ok(f"Wrote manifest: {xml_path.name}")
        else:
            log.dry(f"Would write manifest: {xml_path}")

    # ── Add Folder ────────────────────────────────────────────────────────────

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

        if xml_path.exists():
            try:
                self._apply_xml_order(xml_path)
            except Exception as e:
                log.warn(
                    f"Could not apply XML ordering ({e}); using filesystem order."
                )

        # Restore null-file names: entries that are zero-size and were
        # auto-named "DATA???.dds" should have their name cleared so the
        # round-trip is accurate.
        for entry in self.files:
            if (
                len(entry.data) == 0
                and entry.name.startswith("DATA")
                and entry.name.endswith(".dds")
            ):
                entry.name = ""

        log.info(f"Loaded {len(self.files)} files from {folder.name}/")

    def _apply_xml_order(self, xml_path: Path) -> None:
        """Parse embFiles.xml and reorder self.files to match recorded order."""
        version, entries = read_xml_order(xml_path)

        if version:
            self.version = version

        if not entries:
            return

        by_name: dict[str, EMBFile] = {e.name: e for e in self.files}
        ordered: List[EMBFile] = []

        for filename, orig_name in entries:
            if filename in by_name:
                entry = by_name.pop(filename)
                entry._original_name = orig_name
                if filename != orig_name:
                    entry.name = orig_name
                ordered.append(entry)

        # Append any new files not present in the XML (added after extract)
        for leftover in by_name.values():
            ordered.append(leftover)

        self.files = ordered

    # ── Utilities ─────────────────────────────────────────────────────────────

    def detect_filename_mode(self) -> bool:
        """
        Returns True if the archive should store filenames.
        Returns False if all entries are auto-named DATA???.dds (texture-only).
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
        print(
            f"\n{C.BOLD}{C.WHITE}"
            f"{'#':>4}  {'Name':<40}  {'Size':>12}  {'SHA-1':>10}"
            f"{C.RESET}"
        )
        print(C.GREY + "─" * 74 + C.RESET)

        for entry in self.files:
            sha          = hashlib.sha1(entry.data).hexdigest()[:8] if entry.data else "─" * 8
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
        Used by CLI (--remove NAME). For GUI use remove_entry_by_index().
        """
        name_lower = name.lower()
        for i, entry in enumerate(self.files):
            if entry.name.lower() == name_lower:
                removed = self.files.pop(i)
                for j in range(i, len(self.files)):
                    self.files[j].index = j
                log.ok(f"Removed entry: {removed.name}")
                return True
        log.warn(f"--remove: no entry named '{name}' found.")
        return False

    def remove_entry_by_index(self, index: int) -> bool:
        """
        Remove an entry by its index value. Returns True if found.

        Safer than remove_entry(name) when entries have no filename table
        (all entry.name == ""), which is common in texture-only .emb packs.
        After removal, remaining entries are re-indexed contiguously.
        """
        for i, entry in enumerate(self.files):
            if entry.index == index:
                removed = self.files.pop(i)
                for j in range(i, len(self.files)):
                    self.files[j].index = j
                log.ok(
                    f"Removed entry #{index}"
                    + (f" ({removed.name})" if removed.name else "")
                )
                return True
        log.warn(f"remove_entry_by_index: no entry with index {index} found.")
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
