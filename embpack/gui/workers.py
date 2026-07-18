# workers.py — QThread workers for heavy I/O operations
#
# All workers emit finished(result_or_exception) so the main thread
# can distinguish success from failure with a single isinstance() check.

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from embpack.models import EMB


# ─── Load worker ──────────────────────────────────────────────────────────────

class LoadWorker(QThread):
    """Load an .emb file from disk without blocking the UI."""

    # Emits EMB on success, Exception on failure
    finished = Signal(object)

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self) -> None:
        try:
            emb = EMB.load(self.path)
            self.finished.emit(emb)
        except Exception as e:
            self.finished.emit(e)


# ─── Save worker ──────────────────────────────────────────────────────────────

class SaveWorker(QThread):
    """Save an EMB archive to disk, optionally running verify after."""

    # Emits True on success, Exception on failure
    finished = Signal(object)
    # Emits verify result (True/False) after save+verify, or None if no verify
    verify_result = Signal(object)

    def __init__(
        self,
        emb: EMB,
        path: Path,
        run_verify: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.emb        = emb
        self.path       = path
        self.run_verify = run_verify

    def run(self) -> None:
        try:
            self.emb.save(self.path)
            self.finished.emit(True)
            if self.run_verify:
                ok = self.emb.verify_against(self.path)
                self.verify_result.emit(ok)
            else:
                self.verify_result.emit(None)
        except Exception as e:
            self.finished.emit(e)
            self.verify_result.emit(None)


# ─── Extract worker ───────────────────────────────────────────────────────────

class ExtractWorker(QThread):
    """Extract all entries from an EMB archive to a folder."""

    # progress(current_index, total)
    progress = Signal(int, int)
    # Emits True on success, Exception on failure
    finished = Signal(object)

    def __init__(self, emb: EMB, out_dir: Path, parent=None):
        super().__init__(parent)
        self.emb     = emb
        self.out_dir = out_dir

    def run(self) -> None:
        try:
            total = len(self.emb.files)
            # Monkey-patch a progress hook: wrap each entry's save_to_disk
            # by iterating manually rather than calling emb.extract() directly,
            # so we can emit per-file progress.
            self.out_dir.mkdir(parents=True, exist_ok=True)

            import xml.etree.ElementTree as ET
            from embpack.pathutils import sanitize_filename
            from embpack.xml_manifest import write_pretty_xml

            seen: dict[str, int] = {}
            xml_root = ET.Element(
                "EMB",
                version=self.emb.version,
                debugHaveNames="true" if self.emb._had_names else "false",
            )
            xml_tree = ET.ElementTree(xml_root)

            for i, entry in enumerate(self.emb.files):
                orig_name = entry.name
                if not orig_name:
                    orig_name = (
                        f"DATA{i:03d}.dds" if i < 1000 else
                        f"DATA{i:04d}.dds" if i < 10000 else
                        f"DATA{i:05d}.dds"
                    )
                filename = sanitize_filename(orig_name)
                if filename in seen:
                    seen[filename] += 1
                    stem = Path(filename).stem
                    ext  = Path(filename).suffix
                    filename = f"{stem}_{seen[filename]}{ext}"
                else:
                    seen[filename] = 0

                out_path = self.out_dir / filename
                entry.save_to_disk(out_path)

                ET.SubElement(
                    xml_root, "File",
                    name=orig_name,
                    filename=filename,
                    index=str(i),
                    size=str(len(entry.data)),
                )
                self.progress.emit(i + 1, total)

            write_pretty_xml(xml_tree, self.out_dir / EMB.XML_FILENAME)
            self.finished.emit(True)
        except Exception as e:
            self.finished.emit(e)


# ─── Thumbnail decode worker ──────────────────────────────────────────────────

class ThumbnailWorker(QThread):
    """
    Decode a single DDS entry in the background and emit the result.
    Used for lazy thumbnail generation in grid mode.
    """

    # Emits (entry_index, QImage_or_None)
    decoded = Signal(int, object)

    def __init__(self, index: int, data: bytes, parent=None):
        super().__init__(parent)
        self.index = index
        self.data  = data

    def run(self) -> None:
        from embpack.gui.dds_decoder import decode_dds
        img, _w, _h, _fmt, _err = decode_dds(self.data)
        self.decoded.emit(self.index, img)


# ─── Batch worker ─────────────────────────────────────────────────────────────

class BatchWorker(QThread):
    """
    Process multiple .emb files sequentially (extract or list).
    Emits per-file progress and a log line for each result.
    """

    # progress(current, total, filename)
    progress = Signal(int, int, str)
    # log_line(message, level)  level: "ok" | "error" | "warn"
    log_line = Signal(str, str)
    finished = Signal()

    def __init__(self, paths: list[Path], out_dir: Path, parent=None):
        super().__init__(parent)
        self.paths   = paths
        self.out_dir = out_dir

    def run(self) -> None:
        total = len(self.paths)
        for i, path in enumerate(self.paths):
            self.progress.emit(i + 1, total, path.name)
            try:
                emb  = EMB.load(path)
                dest = self.out_dir / path.stem
                emb.extract(dest)
                self.log_line.emit(
                    f"[OK]   {path.name} → {dest.name}/  ({len(emb.files)} files)",
                    "ok",
                )
            except Exception as e:
                self.log_line.emit(f"[ERR]  {path.name}: {e}", "error")
        self.finished.emit()
