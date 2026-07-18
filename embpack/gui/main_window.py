# main_window.py — MainWindow: layout, menu, toolbar, and action wiring

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, List

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from embpack.models import EMB, EMBFile, EMBLoadError
from embpack.pathutils import backup_file
from embpack.gui.entry_list_widget import EntryListWidget
from embpack.gui.preview_panel import PreviewPanel
from embpack.gui.workers import (
    LoadWorker, SaveWorker, ExtractWorker, ThumbnailWorker, BatchWorker,
)
from embpack.gui.dialogs import (
    ProgressDialog, ExtractDoneDialog, BatchLogDialog,
    show_error, show_warning, ask_confirm,
)

try:
    from embpack import __version__
except ImportError:
    __version__ = "unknown"

_WINDOW_TITLE = "EMBPack"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self._emb:          Optional[EMB]  = None
        self._current_path: Optional[Path] = None
        self._dirty:        bool           = False

        # Active background workers (kept alive while running)
        self._load_worker:    Optional[LoadWorker]    = None
        self._save_worker:    Optional[SaveWorker]    = None
        self._extract_worker: Optional[ExtractWorker] = None
        self._thumb_workers:  List[ThumbnailWorker]   = []
        self._thumb_pending:  set[int]                 = set()  # indices currently being decoded
        self._data_generation: int                     = 0      # track modifications during save

        self._build_ui()
        self._build_menu()
        self._build_toolbar()
        self._build_status_bar()
        self._update_actions()

        self.setAcceptDrops(True)
        self.setWindowTitle(_WINDOW_TITLE)
        self.resize(1100, 700)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)

        self._entry_list = EntryListWidget()
        self._entry_list.setMinimumWidth(200)
        self._entry_list.entry_selected.connect(self._on_entry_selected)
        self._entry_list.thumbnail_needed.connect(self._decode_thumbnail)
        self._entry_list.export_requested.connect(self._on_export_entry)
        self._entry_list.replace_requested.connect(self._on_replace_entry)
        self._entry_list.remove_requested.connect(self._on_remove_entry)

        self._preview = PreviewPanel()
        self._preview.export_requested.connect(self._on_export_entry)
        self._preview.replace_requested.connect(self._on_replace_entry)
        self._preview.remove_requested.connect(self._on_remove_entry)

        splitter.addWidget(self._entry_list)
        splitter.addWidget(self._preview)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)

        root.addWidget(splitter)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        # File
        file_menu = menu_bar.addMenu("&File")
        self._act_open     = file_menu.addAction("&Open…",    self._on_open,     QKeySequence.Open)
        self._act_save     = file_menu.addAction("&Save",     self._on_save,     QKeySequence.Save)
        self._act_save_as  = file_menu.addAction("Save &As…", self._on_save_as,  QKeySequence("Ctrl+Shift+S"))
        file_menu.addSeparator()
        file_menu.addAction("&Quit", self.close, QKeySequence.Quit)

        # Edit
        edit_menu = menu_bar.addMenu("&Edit")
        self._act_add    = edit_menu.addAction("Add &File…",    self._on_add_file)
        self._act_remove = edit_menu.addAction("&Remove Entry", self._on_remove_selected)

        # View
        view_menu = menu_bar.addMenu("&View")
        self._act_list_mode = view_menu.addAction("List Mode",  lambda: self._entry_list._set_mode("list"))
        self._act_grid_mode = view_menu.addAction("Grid Mode",  lambda: self._entry_list._set_mode("grid"))

        # Tools
        tools_menu = menu_bar.addMenu("&Tools")
        self._act_extract_all = tools_menu.addAction("&Extract All…", self._on_extract_all)
        self._act_verify      = tools_menu.addAction("&Verify Archive", self._on_verify)
        tools_menu.addSeparator()
        self._act_batch       = tools_menu.addAction("&Batch Extract…", self._on_batch)

        # Help
        help_menu = menu_bar.addMenu("&Help")
        help_menu.addAction("&About", self._on_about)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.addToolBar(tb)

        tb.addAction(self._act_open)
        tb.addAction(self._act_save)
        tb.addAction(self._act_save_as)
        tb.addSeparator()
        tb.addAction(self._act_extract_all)
        tb.addAction(self._act_add)
        tb.addAction(self._act_remove)
        tb.addSeparator()
        tb.addAction(self._act_verify)
        tb.addAction(self._act_batch)

        # Search box on the right
        tb.addWidget(_spacer())
        tb.addWidget(QLabel("🔍 "))
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search entries…")
        self._search_box.setFixedWidth(200)
        self._search_box.textChanged.connect(self._entry_list.apply_filter)
        tb.addWidget(self._search_box)

    def _build_status_bar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)

        self._status_label = QLabel("No file open")
        sb.addWidget(self._status_label, 1)

        self._warn_label = QLabel("")
        sb.addPermanentWidget(self._warn_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedWidth(180)
        self._progress_bar.setVisible(False)
        sb.addPermanentWidget(self._progress_bar)

    # ── Action state management ────────────────────────────────────────────────

    def _update_actions(self) -> None:
        has_file = self._emb is not None
        for act in (
            self._act_save, self._act_save_as,
            self._act_extract_all, self._act_add, self._act_remove,
            self._act_verify,
            self._act_list_mode, self._act_grid_mode,
        ):
            act.setEnabled(has_file)
        self._entry_list.set_controls_enabled(has_file)

    def _update_status(self) -> None:
        if self._emb is None:
            self._status_label.setText("No file open")
            return
        name   = self._current_path.name if self._current_path else "untitled"
        count  = len(self._emb.files)
        total  = sum(len(e.data) for e in self._emb.files)
        dirty  = " *" if self._dirty else ""
        if total >= 1024 * 1024:
            size_str = f"{total / (1024*1024):.1f} MB"
        elif total >= 1024:
            size_str = f"{total / 1024:.1f} KB"
        else:
            size_str = f"{total} B"
        self._status_label.setText(
            f"{'📄'} {name}{dirty}  —  {count} entries, {size_str}"
        )

    # ── Drag and Drop ─────────────────────────────────────────────────────────

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".emb") for u in urls):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".emb":
                self._open_file(path)
                return
        event.ignore()

    # ── File open ─────────────────────────────────────────────────────────────

    def _on_open(self) -> None:
        if not self._check_unsaved():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open .emb file", "", "EMB Archives (*.emb);;All files (*)"
        )
        if path:
            self._open_file(Path(path))

    def _open_file(self, path: Path) -> None:
        # Stop any previous LoadWorker that is still running — if it finished
        # after the new one, it would overwrite self._emb with stale data.
        if self._load_worker is not None and self._load_worker.isRunning():
            try:
                self._load_worker.finished.disconnect()
            except RuntimeError:
                pass
            self._load_worker.quit()

        # Cancel any in-flight thumbnail workers from the previous file —
        # they would otherwise emit decoded() after the new file is loaded
        # and inject stale thumbnails into the new file's cache/preview.
        for w in self._thumb_workers:
            try:
                w.decoded.disconnect()
            except RuntimeError:
                pass
            if w.isRunning():
                w.quit()
        self._thumb_workers.clear()
        self._thumb_pending.clear()

        self._set_ui_busy(True, "Loading…")
        self._entry_list.clear_all()
        self._preview.clear()
        self._preview.invalidate_cache()

        self._load_worker = LoadWorker(path)
        self._load_worker.finished.connect(self._on_load_finished)
        self._load_worker.start()

    def _on_load_finished(self, result) -> None:
        self._set_ui_busy(False)
        if isinstance(result, Exception):
            show_error(
                "Failed to open file",
                str(result),
                self,
            )
            return

        self._emb          = result
        self._current_path = Path(self._load_worker.path)
        self._dirty        = False

        self._entry_list.populate(self._emb.files)
        self._update_actions()
        self._update_status()
        self.setWindowTitle(
            f"{self._current_path.name} — {_WINDOW_TITLE}"
        )

    # ── Save ──────────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        if self._emb is None:
            return
        if self._current_path is None:
            self._on_save_as()
            return
        self._do_save(self._current_path)

    def _on_save_as(self) -> None:
        if self._emb is None:
            return
        default = str(self._current_path) if self._current_path else ""
        path, _ = QFileDialog.getSaveFileName(
            self, "Save .emb file", default,
            "EMB Archives (*.emb);;All files (*)"
        )
        if path:
            self._do_save(Path(path))

    def _do_save(self, path: Path) -> None:
        # Backup before in-place overwrite (same logic as CLI)
        if path.exists() and self._current_path and path.resolve() == self._current_path.resolve():
            try:
                bak = backup_file(path)
                self._status_label.setText(f"Backup → {bak.name}  (saving…)")
            except Exception as e:
                # Backup failure is non-fatal — warn user but proceed with save
                show_warning(
                    "Backup failed",
                    f"Could not create backup of '{path.name}':\n{e}\n\n"
                    "Save will proceed, but there is no backup copy.",
                    self,
                )

        verify = self._should_verify()
        self._set_ui_busy(True, "Saving…")
        # Disable Save actions to prevent double-save race (backup_file would
        # run twice before the first SaveWorker finishes writing the file).
        self._act_save.setEnabled(False)
        self._act_save_as.setEnabled(False)

        generation_at_save = self._data_generation
        self._save_worker = SaveWorker(self._emb.clone(), path, run_verify=verify)
        self._save_worker.finished.connect(lambda r: self._on_save_finished(r, path, generation_at_save))
        self._save_worker.verify_result.connect(self._on_verify_result)
        self._save_worker.start()

    def _on_save_finished(self, result, path: Path, generation_at_save: int) -> None:
        self._set_ui_busy(False)
        # Re-enable Save actions regardless of success/failure
        self._act_save.setEnabled(self._emb is not None)
        self._act_save_as.setEnabled(self._emb is not None)
        if isinstance(result, Exception):
            show_error("Save failed", str(result), self)
            return
        self._current_path = path
        
        if self._data_generation == generation_at_save:
            self._dirty = False
        else:
            diff = self._data_generation - generation_at_save
            show_warning(
                "Unsaved changes remain",
                f"Saved successfully to '{path.name}', but {diff} new change(s) "
                "occurred during the save process.\n\n"
                "Please save again to include them.",
                self
            )
            # Do NOT clear _dirty flag

        self._update_status()
        self.setWindowTitle(f"{path.name} — {_WINDOW_TITLE}")

    def _on_verify_result(self, result) -> None:
        if result is None:
            return
        if result is True:
            self._warn_label.setText("✅ Verify OK")
        else:
            self._warn_label.setText("❌ Verify FAILED")

    def _should_verify(self) -> bool:
        """Ask user if they want to verify after save. Simple yes/no."""
        return ask_confirm(
            "Verify after save?",
            "Run integrity check after saving?\n"
            "(Reloads the file and compares each entry's SHA-1.)",
            self,
        )

    # ── Extract all ───────────────────────────────────────────────────────────

    def _on_extract_all(self) -> None:
        if self._emb is None:
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Select output folder", ""
        )
        if not out_dir:
            return

        dest = Path(out_dir)
        total = len(self._emb.files)

        dlg = ProgressDialog(
            "Extracting…", f"Extracting {total} files to:\n{dest}",
            determinate=True, parent=self,
        )
        dlg.show()

        self._extract_worker = ExtractWorker(self._emb.clone(), dest)
        self._extract_worker.progress.connect(
            lambda cur, tot: dlg.set_progress(cur, tot)
        )
        self._extract_worker.finished.connect(
            lambda r: self._on_extract_finished(r, dest, total, dlg)
        )
        self._extract_worker.start()

    def _on_extract_finished(
        self, result, dest: Path, total: int, dlg: ProgressDialog
    ) -> None:
        dlg.accept()
        if isinstance(result, Exception):
            show_error("Extraction failed", str(result), self)
            return
        done_dlg = ExtractDoneDialog(dest, total, self)
        done_dlg.exec()

    # ── Add file ──────────────────────────────────────────────────────────────

    def _on_add_file(self) -> None:
        if self._emb is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add file(s) to archive", "",
            "All files (*)"
        )
        for p in paths:
            try:
                self._emb.add_entry(Path(p))
                self._dirty = True
                self._data_generation += 1
            except Exception as e:
                show_error("Add failed", str(e), self)
                return

        self._entry_list.populate(self._emb.files)
        self._update_status()
        # Auto-select the last added entry
        if paths:
            self._entry_list._list.setCurrentRow(
                self._entry_list._list.count() - 1
            )

    # ── Remove entry ──────────────────────────────────────────────────────────

    def _on_remove_selected(self) -> None:
        entry = self._entry_list.selected_entry()
        if entry is None:
            return
        self._on_remove_entry(entry.index)

    def _on_remove_entry(self, index: int) -> None:
        entry = self._entry_by_index(index)
        if entry is None:
            return
        name = entry.name or f"entry #{index}"
        if not ask_confirm(
            "Remove entry",
            f"Remove '{name}' from the archive?\nThis cannot be undone until you save.",
            self,
        ):
            return
        if self._emb.remove_entry_by_index(index):
            self._dirty = True
            self._data_generation += 1
            self._entry_list.populate(self._emb.files)
            self._preview.clear()
            self._update_status()

    # ── Export entry ──────────────────────────────────────────────────────────

    def _on_export_entry(self, index: int) -> None:
        entry = self._entry_by_index(index)
        if entry is None or not entry.data:
            return
        if entry.name:
            default_name = entry.name
        elif index < 1000:
            default_name = f"DATA{index:03d}.dds"
        else:
            default_name = f"DATA{index:05d}.dds"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export entry", default_name, "DDS Files (*.dds);;All files (*)"
        )
        if path:
            try:
                Path(path).write_bytes(entry.data)
            except Exception as e:
                show_error("Export failed", str(e), self)

    # ── Replace entry ─────────────────────────────────────────────────────────

    def _on_replace_entry(self, index: int) -> None:
        entry = self._entry_by_index(index)
        if entry is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Replace entry with…", "", "DDS Files (*.dds);;All files (*)"
        )
        if not path:
            return
        try:
            new_data = Path(path).read_bytes()
            entry.data = new_data
            self._dirty = True
            self._data_generation += 1
            # Invalidate cached thumbnail/preview for this entry
            self._preview.invalidate_cache()
            self._preview.show_entry(entry)
            self._entry_list.populate(self._emb.files)
            self._update_status()
        except Exception as e:
            show_error("Replace failed", str(e), self)

    # ── Verify ────────────────────────────────────────────────────────────────

    def _on_verify(self) -> None:
        if self._emb is None or self._current_path is None:
            show_warning(
                "Cannot verify",
                "Save the file first before verifying.",
                self,
            )
            return
        self._set_ui_busy(True, "Verifying…")
        from PySide6.QtCore import QThread

        class _VerifyWorker(QThread):
            done = Signal(bool)
            def __init__(self, emb, path):
                super().__init__()
                self._emb  = emb
                self._path = path
            def run(self):
                self.done.emit(self._emb.verify_against(self._path))

        w = _VerifyWorker(self._emb, self._current_path)
        w.done.connect(lambda ok: self._finish_verify(ok, w))
        w.start()
        self._verify_worker = w   # keep alive

    def _finish_verify(self, ok: bool, worker) -> None:
        self._set_ui_busy(False)
        if ok:
            self._warn_label.setText("✅ Verify OK")
            QMessageBox.information(self, "Verify", "All entries match — archive is intact.")
        else:
            self._warn_label.setText("❌ Verify FAILED")
            QMessageBox.critical(self, "Verify", "Verification FAILED. Check entries for data corruption.")

    # ── Batch extract ─────────────────────────────────────────────────────────

    def _on_batch(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select .emb files to batch extract", "",
            "EMB Archives (*.emb);;All files (*)"
        )
        if not paths:
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "Select output root folder", ""
        )
        if not out_dir:
            return

        dlg = BatchLogDialog("Batch Extract", parent=self)
        dlg.show()

        worker = BatchWorker([Path(p) for p in paths], Path(out_dir))
        worker.progress.connect(
            lambda cur, tot, name: dlg.set_progress(cur, tot, name)
        )
        worker.log_line.connect(dlg.append_line)
        worker.finished.connect(dlg.mark_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self._batch_worker = worker   # keep alive

    # ── Thumbnail decode ──────────────────────────────────────────────────────

    def _decode_thumbnail(self, index: int) -> None:
        """Spawn a background worker to decode one DDS thumbnail."""
        if self._emb is None:
            return
        if index in self._thumb_pending:
            return  # already in flight, skip duplicate
        entry = self._entry_by_index(index)
        if entry is None or not entry.data:
            return

        self._thumb_pending.add(index)
        w = ThumbnailWorker(index, entry.data)
        w.decoded.connect(self._on_thumbnail_decoded)
        w.finished.connect(lambda: self._thumb_workers.remove(w) if w in self._thumb_workers else None)
        self._thumb_workers.append(w)
        w.start()

    def _on_thumbnail_decoded(self, index: int, img) -> None:
        self._thumb_pending.discard(index)
        self._entry_list.supply_thumbnail(index, img)
        self._preview.supply_cached_image(index, img)

    # ── Entry selection ───────────────────────────────────────────────────────

    def _on_entry_selected(self, entry: EMBFile) -> None:
        self._preview.show_entry(entry)

    # ── About ─────────────────────────────────────────────────────────────────

    def _on_about(self) -> None:
        msg = QMessageBox(self)
        msg.setWindowTitle(f"About EMBPack v{__version__}")
        msg.setText(
            f"<b>EMBPack v{__version__}</b><br>"
            "EMB Archive Tool for Dragon Ball Xenoverse 2<br><br>"
            "<b>Created by:</b> AnchorXV<br>"
            'GitHub: <a href="https://github.com/AnchorXV">github.com/AnchorXV</a><br><br>'
            "<b>Built upon:</b><br>"
            "- Original embpack source code (prior versions)<br>"
            '- LibXenoverse2 by Olganix — <a href="https://github.com/Olganix/LibXenoverse2">github.com/Olganix/LibXenoverse2</a><br><br>'
            "GUI layer built with PySide6.<br>"
            "DDS decode via <i>texture2ddecoder</i>.<br><br>"
            '<b>DBXV2 Modding Community:</b> The Citadel Discord — <a href="https://discord.gg/JmtyGVj">discord.gg/JmtyGVj</a><br><br>'
            "<i>Disclaimer: This tool was developed with the assistance of AI "
            "(Claude by Anthropic) for planning, coding, debugging, and code review. "
            "Please report any issues to the developer or the community Discord above.</i>"
        )
        msg.setTextFormat(Qt.RichText)
        msg.setTextInteractionFlags(Qt.TextBrowserInteraction)
        msg.exec()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _entry_by_index(self, index: int) -> Optional[EMBFile]:
        if self._emb is None:
            return None
        for e in self._emb.files:
            if e.index == index:
                return e
        return None

    def _set_ui_busy(self, busy: bool, label: str = "") -> None:
        self._progress_bar.setVisible(busy)
        if busy:
            self._progress_bar.setRange(0, 0)  # indeterminate
            self._status_label.setText(label)
        else:
            self._progress_bar.setRange(0, 1)
            self._update_status()

    def _check_unsaved(self) -> bool:
        """Return True if it's safe to proceed (no unsaved changes or user confirmed)."""
        if not self._dirty:
            return True
        return ask_confirm(
            "Unsaved changes",
            "There are unsaved changes. Discard and continue?",
            self,
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._dirty and not ask_confirm(
            "Quit with unsaved changes?",
            "There are unsaved changes. Quit anyway?",
            self,
        ):
            event.ignore()
            return
        event.accept()


# ─── Spacer helper ────────────────────────────────────────────────────────────

def _spacer() -> QWidget:
    w = QWidget()
    w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return w
