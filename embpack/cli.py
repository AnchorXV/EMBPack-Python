# cli.py — Argument parser, per-input processors, and main entry point

import sys
import glob
import argparse
from pathlib import Path
from typing import List, Optional

from embpack.colors import C
from embpack.logger import log
from embpack.models import EMB, EMBLoadError
from embpack.pathutils import backup_file

try:
    from embpack import __version__
except ImportError:
    __version__ = "unknown"


# ─── Argument Parser ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="embpack",
        description=(
            f"{C.BOLD}{C.CYAN}embpack{C.RESET} - "
            "EMB Archive Tool for Dragon Ball Xenoverse 2\n"
            "Extract, repack, inspect, and modify .emb archives.\n\n"
            "Auto-detects mode: .emb file -> extract, folder -> repack.\n"
            "Supports glob patterns for batch processing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  embpack shader.emb                   # extract\n"
            "  embpack shader/                       # repack\n"
            "  embpack -l shader.emb                 # list contents\n"
            "  embpack -x shader.emb -o out/         # extract to custom dir\n"
            "  embpack --add new.dds shader.emb      # add file to archive\n"
            "  embpack --remove old.dds shader.emb   # remove file\n"
            "  embpack --verify shader/              # repack + verify\n"
            "  embpack *.emb                         # batch extract\n"
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
        version=f"embpack {__version__}",
    )
    return p


# ─── Per-Input Processors ─────────────────────────────────────────────────────

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

    # Backup before overwriting the original file in-place
    if dest.resolve() == emb_path.resolve():
        bak = backup_file(dest, dry_run=dry_run)
        if dry_run:
            log.dry(f"Would backup: {dest.name} → {bak.name}")
        else:
            log.info(f"Backup created: {bak.name}")

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

    # Backup before overwriting the original file in-place
    if dest.resolve() == emb_path.resolve():
        bak = backup_file(dest, dry_run=dry_run)
        if dry_run:
            log.dry(f"Would backup: {dest.name} → {bak.name}")
        else:
            log.info(f"Backup created: {bak.name}")

    emb.save(dest, dry_run=dry_run)
    return True


# ─── Pause Helper ─────────────────────────────────────────────────────────────

def _pause(args=None, force: bool = False) -> None:
    """Conditionally pause and wait for Enter."""
    if force:
        print("\nPress Enter to continue...")
        input()
        return

    if args is None:
        return

    if getattr(args, "wait", False):
        print("\nPress Enter to continue...")
        input()
    elif not getattr(args, "no_wait", False):
        if log.has_errors:
            print("\nPress Enter to continue...")
            input()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = build_parser()

    # Show help if no arguments at all (e.g. double-click .exe)
    if len(sys.argv) == 1:
        parser.print_help()
        _pause(force=True)
        return 0

    args = parser.parse_args()

    # Logger setup
    log.silent  = args.silent
    log.verbose = args.verbose

    if args.dry_run:
        log.info(
            f"{C.BLUE}{C.BOLD}DRY RUN MODE{C.RESET} — no files will be written."
        )

    # Expand inputs (glob + direct paths)
    resolved: List[Path] = []
    for pattern in args.inputs:
        matches = [Path(p) for p in glob.glob(pattern, recursive=True)]
        if matches:
            resolved.extend(matches)
        else:
            p = Path(pattern)
            if p.exists():
                resolved.append(p)
            else:
                log.warn(f"No match for input: '{pattern}'")

    if not resolved:
        log.error("No valid input files or folders found.")
        _pause(args)
        return 1

    # Batch processing loop
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

        # Dispatch
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

    # Summary
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

if __name__ == '__main__':
    sys.exit(main())
