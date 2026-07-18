# preview_panel.py — Right panel: texture preview + metadata + entry actions

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from embpack.models import EMBFile
from embpack.gui.dds_decoder import decode_dds, _is_image_entry


# Generic file icon SVG (minimal, no external assets needed)
_GENERIC_SVG = b"""<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96'>
  <rect x='16' y='8' width='56' height='72' rx='4' fill='#555' stroke='#888' stroke-width='2'/>
  <polyline points='56,8 72,24 72,24 56,24 56,8' fill='#888'/>
  <rect x='26' y='38' width='36' height='4' rx='2' fill='#aaa'/>
  <rect x='26' y='48' width='28' height='4' rx='2' fill='#aaa'/>
  <rect x='26' y='58' width='20' height='4' rx='2' fill='#aaa'/>
</svg>"""


def _make_generic_pixmap(size: int = 96) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtGui import QPainter
    renderer = QSvgRenderer(_GENERIC_SVG)
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    painter = QPainter(img)
    renderer.render(painter)
    painter.end()
    return QPixmap.fromImage(img)


class PreviewPanel(QWidget):
    """
    Right-hand panel:
    - Texture preview image (scales proportionally)
    - Metadata labels (name, size, index, format, dimensions)
    - Action buttons: Export, Replace, Remove
    """

    # Signals forwarded to MainWindow
    export_requested  = Signal(int)   # entry index
    replace_requested = Signal(int)   # entry index
    remove_requested  = Signal(int)   # entry index

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._current_index: Optional[int] = None
        self._image_cache:   dict[int, Optional[QImage]] = {}

        self._build_ui()
        self.clear()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ── Image preview area ────────────────────────────────────────────────
        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignCenter)
        self._img_label.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding
        )
        self._img_label.setMinimumSize(200, 200)
        self._img_label.setFrameShape(QFrame.StyledPanel)
        self._img_label.setStyleSheet(
            "background-color: #1e1e1e; border: 1px solid #444;"
        )
        root.addWidget(self._img_label, stretch=1)

        # ── Metadata ──────────────────────────────────────────────────────────
        meta_box = QGroupBox("Entry Info")
        meta_layout = QVBoxLayout(meta_box)
        meta_layout.setSpacing(4)

        self._lbl_name   = self._meta_row(meta_layout, "Name:")
        self._lbl_size   = self._meta_row(meta_layout, "Size:")
        self._lbl_index  = self._meta_row(meta_layout, "Index:")
        self._lbl_format = self._meta_row(meta_layout, "Format:")
        self._lbl_dims   = self._meta_row(meta_layout, "Dimensions:")
        self._lbl_error  = QLabel("")
        self._lbl_error.setStyleSheet("color: #e05252; font-style: italic;")
        self._lbl_error.setWordWrap(True)
        meta_layout.addWidget(self._lbl_error)

        root.addWidget(meta_box)

        # ── Action buttons ────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._btn_export  = QPushButton("Export This")
        self._btn_replace = QPushButton("Replace…")
        self._btn_remove  = QPushButton("Remove")
        self._btn_remove.setStyleSheet("color: #e05252;")

        for btn in (self._btn_export, self._btn_replace, self._btn_remove):
            btn.setEnabled(False)
            btn_row.addWidget(btn)

        self._btn_export.clicked.connect(self._on_export)
        self._btn_replace.clicked.connect(self._on_replace)
        self._btn_remove.clicked.connect(self._on_remove)

        root.addLayout(btn_row)

    @staticmethod
    def _meta_row(layout: QVBoxLayout, label: str) -> QLabel:
        row = QHBoxLayout()
        key = QLabel(label)
        key.setFixedWidth(90)
        key.setStyleSheet("font-weight: bold;")
        val = QLabel("—")
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        val.setWordWrap(True)
        row.addWidget(key)
        row.addWidget(val, stretch=1)
        layout.addLayout(row)
        return val

    # ── Public API ────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Reset to empty/placeholder state."""
        self._current_index = None
        self._img_label.setText("No entry selected")
        self._img_label.setPixmap(QPixmap())
        for lbl in (
            self._lbl_name, self._lbl_size, self._lbl_index,
            self._lbl_format, self._lbl_dims,
        ):
            lbl.setText("—")
        self._lbl_error.setText("")
        for btn in (self._btn_export, self._btn_replace, self._btn_remove):
            btn.setEnabled(False)

    def show_entry(self, entry: EMBFile) -> None:
        """Populate the panel with data from *entry*."""
        self._current_index = entry.index

        # FIX: match the DATA###.dds placeholder convention used by the
        # CLI extract() and entry_list_widget.py, instead of a generic
        # "(unnamed/N)" that implies something is wrong with this entry.
        if entry.name:
            display_name = entry.name
        elif entry.index < 1000:
            display_name = f"DATA{entry.index:03d}.dds"
        else:
            display_name = f"DATA{entry.index:05d}.dds"
        self._lbl_name.setText(display_name)
        self._lbl_size.setText(
            f"{len(entry.data):,} bytes" if entry.data else "0 bytes (null entry)"
        )
        self._lbl_index.setText(str(entry.index))

        for btn in (self._btn_export, self._btn_replace, self._btn_remove):
            btn.setEnabled(True)

        if not entry.data:
            self._show_generic("Empty / null entry")
            self._lbl_format.setText("—")
            self._lbl_dims.setText("—")
            return

        if _is_image_entry(entry.name, entry.data):  # FIX: sniff content, not just filename
            self._show_dds(entry)
        else:
            ext = display_name.rsplit(".", 1)[-1].upper() if "." in display_name else "?"
            self._show_generic(f"{ext} file — no preview")
            self._lbl_format.setText(ext)
            self._lbl_dims.setText("—")

    def supply_cached_image(self, index: int, img: Optional[QImage]) -> None:
        """
        Called when a background ThumbnailWorker finishes decoding.
        Updates the display only if this entry is still selected.
        """
        self._image_cache[index] = img
        if index == self._current_index and img is not None:
            self._display_qimage(img)

    def invalidate_cache(self) -> None:
        """Clear decode cache (call when a new file is loaded)."""
        self._image_cache.clear()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _show_dds(self, entry: EMBFile) -> None:
        # Check cache first
        if entry.index in self._image_cache:
            img = self._image_cache[entry.index]
            if img is not None:
                self._display_qimage(img)
            else:
                self._show_generic("Decode failed (see below)")
            return

        # Decode synchronously for the preview panel (it's only one texture)
        img, w, h, fmt, err = decode_dds(entry.data)
        self._image_cache[entry.index] = img

        self._lbl_format.setText(fmt)
        self._lbl_dims.setText(f"{w} × {h}" if (w and h) else "—")

        if err:
            self._lbl_error.setText(f"Preview error: {err}")
            self._show_generic("Cannot preview")
        else:
            self._lbl_error.setText("")
            self._display_qimage(img)

    def _display_qimage(self, img: QImage) -> None:
        px = QPixmap.fromImage(img)
        scaled = px.scaled(
            self._img_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._img_label.setPixmap(scaled)

    def _show_generic(self, reason: str = "") -> None:
        px = _make_generic_pixmap(96)
        self._img_label.setPixmap(px)
        if reason:
            self._lbl_error.setText(reason)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Re-scale the current pixmap when the panel is resized
        if self._current_index is not None:
            img = self._image_cache.get(self._current_index)
            if img is not None:
                self._display_qimage(img)

    # ── Button slots ──────────────────────────────────────────────────────────

    def _on_export(self) -> None:
        if self._current_index is not None:
            self.export_requested.emit(self._current_index)

    def _on_replace(self) -> None:
        if self._current_index is not None:
            self.replace_requested.emit(self._current_index)

    def _on_remove(self) -> None:
        if self._current_index is not None:
            self.remove_requested.emit(self._current_index)