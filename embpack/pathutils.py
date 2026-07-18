# pathutils.py — Filename sanitization, safe output path resolution, and backup

import os
import re
import shutil
from pathlib import Path


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

    NOTE: This is a defense-in-depth utility. In the current codebase,
    sanitize_filename() already strips all directory components via
    os.path.basename(), so path traversal is blocked before this is
    called. This function is kept as a public API for external callers
    and as an additional safety layer should the call chain change.
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


def backup_file(path: Path, dry_run: bool = False) -> Path:
    """
    Create a versioned backup of *path* before it gets overwritten.

    Naming scheme:
        shader.emb  →  shader.emb.bak      (first backup)
        shader.emb  →  shader.emb.bak.2    (second)
        shader.emb  →  shader.emb.bak.3    (third) …

    The plain .bak slot is always the *most recent* backup so that a quick
    manual restore is just renaming one file.  Older copies are rotated:
        existing .bak   → .bak.2
        existing .bak.2 → .bak.3
        … up to the highest slot found, then shift everything up by 1.

    Returns the Path of the backup that was just written (or would be written
    in dry-run mode).  Does nothing and returns *path* if *path* does not
    exist yet (nothing to back up).
    """
    if not path.exists():
        return path

    base = Path(str(path) + ".bak")

    if not dry_run:
        # Collect all existing versioned slots: .bak, .bak.2, .bak.3 …
        existing: list[int] = []
        if base.exists():
            existing.append(1)
        n = 2
        while Path(f"{base}.{n}").exists():
            existing.append(n)
            n += 1

        # Rotate upward: .bak.N → .bak.(N+1), …, .bak → .bak.2
        for slot in sorted(existing, reverse=True):
            src  = base if slot == 1 else Path(f"{base}.{slot}")
            dest = Path(f"{base}.{slot + 1}")
            src.rename(dest)

        # Write fresh backup into the .bak slot
        shutil.copy2(path, base)

    return base
