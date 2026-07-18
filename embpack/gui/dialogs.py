# dialogs.py — Reusable dialogs: progress, error, confirmation, batch

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


# ─── Progress dialog ──────────────────────────────────────────────────────────

class ProgressDialog(QDialog):
    """
    Simple modal progress dialog.
    Pass determinate=False for an indeterminate (busy) bar.
    """

    cancelled = Signal()

    def __init__(
        self,
        title: str,
        label: str,
        determinate: bool = True,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumWidth(380)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)

        self._label = QLabel(label)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        self._bar = QProgressBar()
        if determinate:
            self._bar.setRange(0, 100)
            self._bar.setValue(0)
        else:
            self._bar.setRange(0, 0)   # indeterminate / busy
        layout.addWidget(self._bar)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._cancel_btn)
        layout.addLayout(btn_layout)

    def set_label(self, text: str) -> None:
        self._label.setText(text)

    def set_progress(self, value: int, total: int) -> None:
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(value)

    def _on_cancel(self) -> None:
        self.cancelled.emit()
        self.reject()


# ─── Error dialog ─────────────────────────────────────────────────────────────

def show_error(title: str, message: str, parent: Optional[QWidget] = None) -> None:
    """Show a modal error message box with the exact exception text."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Critical)
    box.setText(message)
    box.exec()


def show_warning(title: str, message: str, parent: Optional[QWidget] = None) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning)
    box.setText(message)
    box.exec()


def show_info(title: str, message: str, parent: Optional[QWidget] = None) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Information)
    box.setText(message)
    box.exec()


def ask_confirm(
    title: str,
    message: str,
    parent: Optional[QWidget] = None,
) -> bool:
    """Ask yes/no. Returns True if user clicked Yes."""
    result = QMessageBox.question(
        parent,
        title,
        message,
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    return result == QMessageBox.Yes


# ─── Extract-complete dialog ──────────────────────────────────────────────────

class ExtractDoneDialog(QDialog):
    """Shown after extract finishes — includes an 'Open Folder' button."""

    def __init__(
        self,
        out_dir: Path,
        file_count: int,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Extraction Complete")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._out_dir = out_dir

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"Extracted {file_count} file(s) to:\n{out_dir}"
            )
        )

        buttons = QDialogButtonBox()
        open_btn = QPushButton("Open Folder")
        open_btn.clicked.connect(self._open_folder)
        buttons.addButton(open_btn, QDialogButtonBox.ActionRole)
        buttons.addButton(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _open_folder(self) -> None:
        os.startfile(str(self._out_dir))   # Windows only; safe for this project


# ─── Batch log dialog ─────────────────────────────────────────────────────────

class BatchLogDialog(QDialog):
    """
    Shows a read-only log panel for batch operations.
    Lines are colour-coded: ok=green, error=red, warn=yellow.
    """

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(600, 400)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        layout.addWidget(self._progress)

        self._progress_label = QLabel("Processing…")
        layout.addWidget(self._progress_label)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self._log)

        self._close_btn = QPushButton("Close")
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self.accept)
        layout.addWidget(self._close_btn, alignment=Qt.AlignRight)

    def append_line(self, text: str, level: str = "info") -> None:
        colors = {
            "ok":    "#4ec94e",
            "error": "#e05252",
            "warn":  "#e0c252",
        }
        colour = colors.get(level, "#cccccc")
        self._log.appendHtml(f'<span style="color:{colour}">{text}</span>')

    def set_progress(self, current: int, total: int, filename: str = "") -> None:
        self._progress.setRange(0, total)
        self._progress.setValue(current)
        self._progress_label.setText(
            f"Processing {current}/{total}: {filename}"
        )

    def mark_done(self) -> None:
        self._progress_label.setText("Done.")
        self._close_btn.setEnabled(True)
