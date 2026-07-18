# entry_list_widget.py — Left panel: list/grid entry display with search

from __future__ import annotations

from typing import Optional, List

from PySide6.QtCore import Qt, Signal, QSize, QTimer
from PySide6.QtGui import QIcon, QPixmap, QImage
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from embpack.models import EMBFile
from embpack.gui.dds_decoder import _is_image_entry


# ─── Roles ────────────────────────────────────────────────────────────────────

_ROLE_INDEX = Qt.UserRole          # stores EMBFile.index (int)
_ROLE_NAME  = Qt.UserRole + 1      # stores display name (str)


def _format_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024*1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ─── Widget ───────────────────────────────────────────────────────────────────

class EntryListWidget(QWidget):
    """
    Left panel with two view modes:
    - List mode: QListWidget in ListMode (name + size columns via item text)
    - Grid mode: QListWidget in IconMode (96×96 thumbnails)

    Signals:
        entry_selected(EMBFile)  — user clicked an entry
        entries_changed()        — after add/remove (so MainWindow can mark dirty)
    """

    entry_selected    = Signal(object)   # EMBFile
    thumbnail_needed  = Signal(int)      # index whose thumbnail should be decoded
    export_requested  = Signal(int)      # entry index — right-click → Export
    replace_requested = Signal(int)      # entry index — right-click → Replace
    remove_requested  = Signal(int)      # entry index — right-click → Remove

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)

        self._entries:    List[EMBFile] = []
        self._icon_cache: dict[int, QIcon] = {}    # index → QIcon
        self._generic_icon: Optional[QIcon] = None
        self._view_mode = "list"   # "list" or "grid"

        self._build_ui()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # ── Toolbar: view-mode toggle ──────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("View:"))

        self._btn_list = QPushButton("List")
        self._btn_list.setCheckable(True)
        self._btn_list.setChecked(True)
        self._btn_list.setFixedHeight(24)
        self._btn_list.clicked.connect(lambda: self._set_mode("list"))

        self._btn_grid = QPushButton("Grid")
        self._btn_grid.setCheckable(True)
        self._btn_grid.setChecked(False)
        self._btn_grid.setFixedHeight(24)
        self._btn_grid.clicked.connect(lambda: self._set_mode("grid"))

        top_row.addWidget(self._btn_list)
        top_row.addWidget(self._btn_grid)
        top_row.addStretch()

        self._count_label = QLabel("0 entries")
        self._count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top_row.addWidget(self._count_label)
        root.addLayout(top_row)

        # ── List widget ────────────────────────────────────────────────────
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list.currentItemChanged.connect(self._on_selection_changed)
        self._list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.setAcceptDrops(False)  # handled at MainWindow level

        # Debounce timer for scroll-triggered thumbnail requests.
        # A persistent QTimer with start() (restart = cancel previous)
        # ensures only ONE call fires after scrolling settles.
        self._scroll_debounce = QTimer()
        self._scroll_debounce.setSingleShot(True)
        self._scroll_debounce.setInterval(80)
        self._scroll_debounce.timeout.connect(self._request_visible_thumbnails)
        self._list.verticalScrollBar().valueChanged.connect(
            self._on_scroll_changed
        )

        # Coalescing timer for batching setIcon() updates from background
        # ThumbnailWorkers.  Multiple rapid supply_thumbnail() calls buffer
        # their icons here; when the timer fires, all are applied in one
        # pass with setUpdatesEnabled(False), preventing the incremental
        # relayout that causes ghosting/doubling in IconMode.
        self._pending_icons: dict[int, QIcon] = {}
        self._icon_flush_timer = QTimer()
        self._icon_flush_timer.setSingleShot(True)
        self._icon_flush_timer.setInterval(60)
        self._icon_flush_timer.timeout.connect(self._flush_pending_icons)

        root.addWidget(self._list)

        self._set_mode("list")

    # ── Public API ────────────────────────────────────────────────────────────

    def set_controls_enabled(self, enabled: bool) -> None:
        self._btn_list.setEnabled(enabled)
        self._btn_grid.setEnabled(enabled)

    def populate(self, entries: List[EMBFile]) -> None:
        """Replace the current list with *entries*."""
        self._entries = list(entries)
        self._icon_cache.clear()
        self._refresh_list(filter_text="")
        self._count_label.setText(f"{len(entries)} entries")

    def clear_all(self) -> None:
        self._entries = []
        self._icon_cache.clear()
        self._list.clear()
        self._count_label.setText("0 entries")

    def apply_filter(self, text: str) -> None:
        """Show/hide items based on substring match (case-insensitive)."""
        text = text.lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            name = item.data(_ROLE_NAME) or ""
            item.setHidden(text not in name.lower())

    def supply_thumbnail(self, index: int, img: Optional[QImage]) -> None:
        """
        Called when a background worker finishes decoding index's thumbnail.
        Buffers the icon update and coalesces with other pending updates.
        """
        if img is None:
            return
        icon = QIcon(QPixmap.fromImage(
            img.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        ))
        self._icon_cache[index] = icon
        self._pending_icons[index] = icon
        self._icon_flush_timer.start()  # restart = coalesce with other pending

    def selected_entry(self) -> Optional[EMBFile]:
        item = self._list.currentItem()
        if item is None:
            return None
        idx = item.data(_ROLE_INDEX)
        return self._entry_by_index(idx)

    # ── Mode toggle ───────────────────────────────────────────────────────────

    def _set_mode(self, mode: str) -> None:
        self._view_mode = mode
        self._btn_list.setChecked(mode == "list")
        self._btn_grid.setChecked(mode == "grid")

        if mode == "grid":
            self._list.setViewMode(QListWidget.IconMode)
            self._list.setMovement(QListWidget.Static)   # prevent free-positioning glitches
            self._list.setIconSize(QSize(96, 96))
            self._list.setGridSize(QSize(120, 120))
            self._list.setResizeMode(QListWidget.Adjust)
            self._list.setWrapping(True)
            self._list.setSpacing(4)
            self._list.setWordWrap(True)
            self._list.setUniformItemSizes(True)
        else:
            self._list.setViewMode(QListWidget.ListMode)
            self._list.setMovement(QListWidget.Static)
            self._list.setIconSize(QSize(20, 20))
            self._list.setGridSize(QSize())
            self._list.setResizeMode(QListWidget.Fixed)
            self._list.setWrapping(False)
            self._list.setSpacing(0)
            self._list.setWordWrap(False)
            self._list.setUniformItemSizes(False)

        # Re-populate items to apply icon size / text layout
        current_idx = None
        item = self._list.currentItem()
        if item:
            current_idx = item.data(_ROLE_INDEX)

        self._refresh_list(filter_text="")

        # Restore selection
        if current_idx is not None:
            for i in range(self._list.count()):
                if self._list.item(i).data(_ROLE_INDEX) == current_idx:
                    self._list.setCurrentRow(i)
                    break

        # Request thumbnails lazily in grid mode
        if mode == "grid":
            QTimer.singleShot(0, self._request_visible_thumbnails)

    # ── List population ───────────────────────────────────────────────────────

    def _refresh_list(self, filter_text: str = "") -> None:
        # Suspend repaints while bulk-adding items.  Without this, each
        # addItem() in IconMode triggers an incremental relayout that
        # causes stale geometry and visual doubling/ghosting.
        self._list.setUpdatesEnabled(False)
        self._list.clear()
        for entry in self._entries:
            # FIX: many real EMB variants (texture-only packs, .dyt files)
            # have no filename table at all -- every entry.name is "". Show
            # the same DATA###.dds placeholder the CLI/extract() convention
            # uses, instead of a generic "(unnamed/N)" that implies something
            # is wrong with this specific file.
            if entry.name:
                display_name = entry.name
            elif entry.index < 1000:
                display_name = f"DATA{entry.index:03d}.dds"
            else:
                display_name = f"DATA{entry.index:05d}.dds"
            size_str = _format_size(len(entry.data))

            if self._view_mode == "list":
                text = f"{display_name}  —  {size_str}"
            else:
                # Grid: shorter label under the thumbnail
                text = display_name if len(display_name) <= 18 else display_name[:16] + "…"

            item = QListWidgetItem(text)
            item.setData(_ROLE_INDEX, entry.index)
            item.setData(_ROLE_NAME, display_name)
            item.setToolTip(
                f"Index: {entry.index}\nName: {display_name}\nSize: {size_str}"
            )

            # Set icon
            if entry.index in self._icon_cache:
                item.setIcon(self._icon_cache[entry.index])
            elif _is_image_entry(entry.name, entry.data):  # FIX: sniff content, not just filename
                item.setIcon(self._placeholder_icon())
            else:
                item.setIcon(self._generic_file_icon())

            if filter_text and filter_text.lower() not in display_name.lower():
                item.setHidden(True)

            self._list.addItem(item)
        self._list.setUpdatesEnabled(True)
        self._list.doItemsLayout()  # force a clean, complete layout pass

    def _placeholder_icon(self) -> QIcon:
        """Grey checkerboard placeholder for unloaded DDS thumbnails."""
        img = QImage(96, 96, QImage.Format_ARGB32)
        img.fill(0xFF2A2A2A)
        # Draw a simple 4×4 checkerboard
        tile = 12
        for row in range(0, 96, tile):
            for col in range(0, 96, tile):
                if (row // tile + col // tile) % 2 == 0:
                    for y in range(row, min(row + tile, 96)):
                        for x in range(col, min(col + tile, 96)):
                            img.setPixel(x, y, 0xFF3A3A3A)
        return QIcon(QPixmap.fromImage(img))

    def _generic_file_icon(self) -> QIcon:
        if self._generic_icon is None:
            img = QImage(96, 96, QImage.Format_ARGB32)
            img.fill(0xFF2A2A2A)
            self._generic_icon = QIcon(QPixmap.fromImage(img))
        return self._generic_icon

    # ── Batched icon flush ─────────────────────────────────────────────────────

    def _flush_pending_icons(self) -> None:
        """Apply all buffered icon updates in one pass (painting suspended)."""
        if not self._pending_icons:
            return
        self._list.setUpdatesEnabled(False)
        for i in range(self._list.count()):
            item = self._list.item(i)
            idx = item.data(_ROLE_INDEX)
            if idx in self._pending_icons:
                item.setIcon(self._pending_icons[idx])
        self._pending_icons.clear()
        self._list.setUpdatesEnabled(True)
        self._list.doItemsLayout()

    # ── Lazy thumbnail requests ───────────────────────────────────────────────

    def _request_visible_thumbnails(self) -> None:
        """Emit thumbnail_needed for entries visible in the viewport."""
        if self._view_mode != "grid":
            return
        viewport_rect = self._list.viewport().rect()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.isHidden():
                continue
            rect = self._list.visualItemRect(item)
            if viewport_rect.intersects(rect):
                idx = item.data(_ROLE_INDEX)
                if idx not in self._icon_cache:
                    self.thumbnail_needed.emit(idx)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_selection_changed(
        self,
        current: Optional[QListWidgetItem],
        _previous,
    ) -> None:
        if current is None:
            return
        idx = current.data(_ROLE_INDEX)
        entry = self._entry_by_index(idx)
        if entry is not None:
            self.entry_selected.emit(entry)

    def _on_context_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu
        item = self._list.itemAt(pos)
        if item is None:
            return
        idx = item.data(_ROLE_INDEX)
        entry = self._entry_by_index(idx)
        if entry is None:
            return

        menu = QMenu(self)
        act_export  = menu.addAction("Export This File…")
        act_replace = menu.addAction("Replace With…")
        menu.addSeparator()
        act_remove  = menu.addAction("Remove Entry")
        act_remove.setIcon(self.style().standardIcon(
            self.style().StandardPixmap.SP_TrashIcon
        ))

        action = menu.exec(self._list.viewport().mapToGlobal(pos))
        if action == act_export:
            self.export_requested.emit(idx)
        elif action == act_replace:
            self.replace_requested.emit(idx)
        elif action == act_remove:
            self.remove_requested.emit(idx)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _entry_by_index(self, index: int) -> Optional[EMBFile]:
        for e in self._entries:
            if e.index == index:
                return e
        return None

    def _on_scroll_changed(self, _value: int) -> None:
        """Triggered by vertical scrollbar movement — request thumbnails for newly visible entries."""
        if self._view_mode == "grid":
            self._scroll_debounce.start()  # restart cancels any pending fire