# EMBPack

**EMBPack** is a modding toolkit for Dragon Ball Xenoverse 2's `.emb` (Entity Model Bundle) archive format. It lets you extract, inspect, repack, and preview the contents of `.emb` files through either a command-line tool or a full desktop GUI.

Available in two version:
- **EMBPack CLI** — scriptable, batch-friendly, no dependencies beyond Python's standard library.
- **EMBPack GUI** — PySide6-based desktop app with texture preview, drag-and-drop, and batch extraction.

> Prebuilt Windows executables (`EMBPack.exe` and `EMBPackCLI.exe`) are published under [Releases](../../releases) — you don't need Python installed to use them.

---

## Features

- **Extract** `.emb` archives to a folder, preserving original file order and names via a generated `embFiles.xml` manifest
- **Repack** a folder back into a valid `.emb` archive, byte-compatible with the original game format
- **Roundtrip verification** — save-and-reload check to catch corruption before it reaches the game
- **Automatic backup** before overwriting an existing `.emb` file
- **DDS texture preview** (GUI) — supports BC1/BC2/BC3/BC4/BC5/BC6H/BC7 and uncompressed RGBA/BGRA formats
- **Grid & list view** with lazy-loaded thumbnails for large archives (200+ entries)
- **Duplicate filename handling** (e.g. `pbind.emb`-style archives with repeated names)
- **Batch extract** multiple `.emb` files at once
- Safe handling of texture-only archives with no filename table (common for `.dyt` palette files, shader packs, etc.)

---

## Installation

### Option 1 — Prebuilt executable (recommended for most users)

Download the latest `EMBPack.exe` (GUI) or `EMBPackCLI.exe` (CLI) from the [Releases](../../releases) page. No installation needed — just run it.

### Option 2 — From source

Requires Python 3.10+.

```bash
git clone https://github.com/AnchorXV/EMBPack-Python.git
cd EMBPack
pip install -r requirements-gui.txt   # only needed for the GUI
```

---

## Usage

### CLI

```bash
# Extract a .emb archive
python -m embpack.cli path/to/character.emb

# Repack a folder back into a .emb archive
python -m embpack.cli path/to/character/

# Repack without roundtrip verification (faster, less safe)
python -m embpack.cli path/to/character/ --no-verify

# See all available options
python -m embpack.cli --help
```

### GUI

```bash
python -m embpack.gui
```

Or run the prebuilt `EMBPack.exe` directly. Open a `.emb` file via **File → Open…** or drag-and-drop it onto the window.

---

## Building from source (Windows, via Nuitka)

```bash
pip install nuitka

# GUI build
nuitka --standalone --onefile ^
  --enable-plugin=pyside6 ^
  --windows-console-mode=disable ^
  --include-package=texture2ddecoder ^
  --company-name="AnchorXV" ^
  --product-name="EMBPack" ^
  --output-filename=EMBPack.exe ^
  --output-dir=dist ^
  embpack/gui/app.py

# CLI build
nuitka --standalone --onefile ^
  --company-name="AnchorXV" ^
  --product-name="EMBPack CLI" ^
  --output-filename=EMBPackCLI.exe ^
  --output-dir=dist ^
  embpack/cli.py
```

---

## Project structure

```
embpack/
├── gui/                  # PySide6 desktop application
│   ├── app.py            # Entry point (python -m embpack.gui)
│   ├── main_window.py     # Main window, menus, and action wiring
│   ├── entry_list_widget.py
│   ├── preview_panel.py
│   ├── dds_decoder.py     # DDS → QImage decoding (texture2ddecoder)
│   ├── workers.py         # Background QThread workers
│   └── dialogs.py
├── models.py              # Core EMB / EMBFile data model
├── binary_io.py           # Low-level binary reader/writer
├── pathutils.py           # Filename sanitization, backup helper
├── xml_manifest.py        # embFiles.xml read/write
├── cli.py                 # Command-line entry point
└── constants.py
```

---

## Credits

**Created by:** [AnchorXV](https://github.com/AnchorXV)

**Built upon:**
- Original `embpack` source code (prior versions)
- [LibXenoverse2](https://github.com/Olganix/LibXenoverse2) by Olganix

**Community:** [The Citadel Discord](https://discord.gg/JmtyGVj)

---

## Disclaimer

This tool was developed with the assistance of AI (Claude by Anthropic) for planning, coding, debugging, and code review. It is provided as-is for the Dragon Ball Xenoverse 2 modding community. Please report issues via GitHub Issues or the Discord server linked above.

---

## License

Licensed under the [MIT License](LICENSE).
