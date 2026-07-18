# xml_manifest.py — embFiles.xml read/write helpers

import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from typing import List, Tuple


def write_pretty_xml(tree: ET.ElementTree, path: Path) -> None:
    """Write an ElementTree to disk with indented formatting."""
    raw = ET.tostring(tree.getroot(), encoding="unicode")
    reparsed = minidom.parseString(raw)
    pretty = reparsed.toprettyxml(indent="  ", encoding="utf-8")
    path.write_bytes(pretty)


def read_xml_order(xml_path: Path) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Parse embFiles.xml and return the archive version and a list of
    (filename, original_name) pairs in their recorded order.

    Returns:
        version (str): archive version string, or "" if not present.
        entries (list): list of (disk_filename, original_name) tuples.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    version = root.get("version", "")
    entries: List[Tuple[str, str]] = []

    for node in root.findall("File"):
        fname = node.get("filename") or node.get("name", "")
        name  = node.get("name", fname)
        entries.append((fname, name))

    return version, entries
