# app.py — Entry point: create QApplication and show MainWindow
#
# Run as:
#   python -m embpack.gui
# or:
#   embpack-gui   (if installed via setup.py / pyproject.toml)

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor

from embpack.gui.main_window import MainWindow


def run(args: list[str] | None = None) -> int:
    if args is None:
        args = sys.argv

    app = QApplication(args)
    app.setApplicationName("EMBPack")
    app.setOrganizationName("embpack")

    # Dark-ish palette — close to VS Code dark
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(45, 45, 45))
    palette.setColor(QPalette.WindowText,      QColor(220, 220, 220))
    palette.setColor(QPalette.Base,            QColor(30, 30, 30))
    palette.setColor(QPalette.AlternateBase,   QColor(50, 50, 50))
    palette.setColor(QPalette.ToolTipBase,     QColor(30, 30, 30))
    palette.setColor(QPalette.ToolTipText,     QColor(220, 220, 220))
    palette.setColor(QPalette.Text,            QColor(220, 220, 220))
    palette.setColor(QPalette.Button,          QColor(60, 60, 60))
    palette.setColor(QPalette.ButtonText,      QColor(220, 220, 220))
    palette.setColor(QPalette.BrightText,      QColor(255, 255, 255))
    palette.setColor(QPalette.Link,            QColor(100, 180, 255))
    palette.setColor(QPalette.Highlight,       QColor(70, 120, 200))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    window = MainWindow()
    window.show()

    # If a .emb path was passed as CLI argument, open it immediately
    for arg in args[1:]:
        p = Path(arg)
        if p.is_file() and p.suffix.lower() == ".emb":
            window._open_file(p)
            break

    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
