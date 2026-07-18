# dds_decoder.py — DDS texture decode via texture2ddecoder -> QImage
#
# Defensive: any decode failure returns (None, "error reason") instead of
# raising, so the GUI can show a placeholder without crashing.

from __future__ import annotations

import struct
from typing import Optional, Tuple

try:
    from PySide6.QtGui import QImage
except ImportError:
    QImage = None  # type: ignore

try:
    import texture2ddecoder as t2d
    _T2D_AVAILABLE = True
except ImportError:
    _T2D_AVAILABLE = False


# ─── DDS constants ────────────────────────────────────────────────────────────

_DDS_MAGIC          = b"DDS "
_DDPF_FOURCC        = 0x00000004
_DDPF_RGB           = 0x00000040
_DDPF_RGB_ALPHA     = 0x00000041   # DDPF_RGB | DDPF_ALPHAPIXELS
_DDPF_LUMINANCE     = 0x00020000
_FOURCC_DXT1        = b"DXT1"
_FOURCC_DXT3        = b"DXT3"
_FOURCC_DXT5        = b"DXT5"
_FOURCC_ATI1        = b"ATI1"
_FOURCC_ATI2        = b"ATI2"
_FOURCC_BC4U        = b"BC4U"
_FOURCC_BC5U        = b"BC5U"
_FOURCC_DX10        = b"DX10"

# DXGI_FORMAT values used in DX10 extended header
_DXGI_FORMAT_MAP: dict[int, str] = {
    71:  "BC1_UNORM",
    72:  "BC1_UNORM_SRGB",
    74:  "BC2_UNORM",
    75:  "BC2_UNORM_SRGB",
    77:  "BC3_UNORM",
    78:  "BC3_UNORM_SRGB",
    80:  "BC4_UNORM",
    83:  "BC5_UNORM",
    95:  "BC6H_UF16",
    96:  "BC6H_SF16",
    98:  "BC7_UNORM",
    99:  "BC7_UNORM_SRGB",
    28:  "R8G8B8A8_UNORM",
    29:  "R8G8B8A8_UNORM_SRGB",
    87:  "B8G8R8A8_UNORM",
    91:  "B8G8R8A8_UNORM_SRGB",
    61:  "R8_UNORM",
}


# ─── Header parsing ────────────────────────────────────────────────────────────

def _parse_dds_header(data: bytes) -> Tuple[int, int, str]:
    """
    Parse DDS header and return (width, height, format_name).
    Raises ValueError on malformed data.
    """
    if len(data) < 128 or data[:4] != _DDS_MAGIC:
        raise ValueError("Not a DDS file or data too short.")

    # DDS_HEADER starts at offset 4
    # Offsets relative to byte 4 (start of DDS_HEADER struct):
    #   dwSize       +0   (should be 124)
    #   dwFlags      +4
    #   dwHeight     +8
    #   dwWidth      +12
    #   ...
    #   ddspf        +72  (32-byte pixel-format struct)
    #     dwFlags    +72
    #     dwFourCC   +76
    h_off = 4
    _ = struct.unpack_from("<I", data, h_off)[0]   # dwSize: skip, advance only
    height     = struct.unpack_from("<I", data, h_off + 8)[0]
    width      = struct.unpack_from("<I", data, h_off + 12)[0]
    # ddspf (DDS_PIXELFORMAT) starts at h_off+72, layout:
    #   +0  dwSize (always 32)   +4  dwFlags   +8  dwFourCC   +12 dwRGBBitCount …
    # BUG 1 FIX: pf_flags is at h_off+72+4 = h_off+76, fourcc at h_off+72+8 = h_off+80
    # (previously read h_off+72 for pf_flags which is dwSize=32, never has DDPF_FOURCC set)
    pf_flags   = struct.unpack_from("<I", data, h_off + 76)[0]   # dwFlags
    fourcc     = data[h_off + 80 : h_off + 84]                   # dwFourCC

    if pf_flags & _DDPF_FOURCC:
        if fourcc == _FOURCC_DX10:
            # Extended DX10 header starts at offset 4 + 124 = 128
            if len(data) < 148:
                raise ValueError("DX10 extended header truncated.")
            dxgi_fmt = struct.unpack_from("<I", data, 128)[0]
            fmt_name = _DXGI_FORMAT_MAP.get(dxgi_fmt, f"DXGI_{dxgi_fmt}")
        elif fourcc == _FOURCC_DXT1:
            fmt_name = "BC1"
        elif fourcc == _FOURCC_DXT3:
            fmt_name = "BC2"
        elif fourcc == _FOURCC_DXT5:
            fmt_name = "BC3"
        elif fourcc in (_FOURCC_ATI1, _FOURCC_BC4U):
            fmt_name = "BC4"
        elif fourcc in (_FOURCC_ATI2, _FOURCC_BC5U):
            fmt_name = "BC5"
        else:
            fmt_name = fourcc.decode("ascii", errors="replace").strip("\x00")
    elif pf_flags & _DDPF_RGB:
        fmt_name = "RGBA" if (pf_flags & _DDPF_RGB_ALPHA) else "RGB"
    elif pf_flags & _DDPF_LUMINANCE:
        fmt_name = "Luminance"
    else:
        fmt_name = "Unknown"

    return width, height, fmt_name


def _is_image_entry(name: str, data: bytes = b"") -> bool:
    """
    Return True if this entry looks like a DDS image.

    FIX: previously checked only `name.lower().endswith(".dds")`. Many real
    EMB variants (texture-only packs, .dyt palette files, shader packs) have
    NO filename table at all — every entry's name is "" — so this check
    always returned False for exactly the files where image preview matters
    most. Now sniffs content (DDS magic bytes) when data is available, and
    falls back to the name check only when no data was supplied (e.g. a
    lightweight metadata-only call site that hasn't loaded bytes yet).
    """
    if data:
        return data[:4] == _DDS_MAGIC
    return name.lower().endswith(".dds")


# ─── Decode dispatch ──────────────────────────────────────────────────────────

def decode_dds(data: bytes) -> Tuple[Optional["QImage"], int, int, str, Optional[str]]:
    """
    Attempt to decode DDS bytes into a QImage.

    Returns:
        (qimage, width, height, format_name, error_message)
        On success: qimage is a valid QImage, error_message is None.
        On failure: qimage is None, error_message describes the problem.
    """
    if not _T2D_AVAILABLE:
        return None, 0, 0, "?", "texture2ddecoder not installed"

    if QImage is None:
        return None, 0, 0, "?", "PySide6 not available"

    try:
        width, height, fmt = _parse_dds_header(data)
    except ValueError as e:
        return None, 0, 0, "?", str(e)

    if width == 0 or height == 0:
        return None, width, height, fmt, "Zero-dimension texture"

    try:
        rgba = _decode_format(data, width, height, fmt)
    except Exception as e:
        return None, width, height, fmt, f"Decode failed ({fmt}): {e}"

    if rgba is None:
        return None, width, height, fmt, f"Format not supported: {fmt}"

    # Build QImage from raw RGBA bytes (no temp file round-trip)
    img = QImage(rgba, width, height, width * 4, QImage.Format_RGBA8888)
    if img.isNull():
        return None, width, height, fmt, "QImage construction failed"

    return img.copy(), width, height, fmt, None   # .copy() detaches from raw bytes


def _decode_format(data: bytes, width: int, height: int, fmt: str) -> Optional[bytes]:
    """
    Dispatch to the correct texture2ddecoder function based on fmt string.
    Returns raw RGBA bytes, or None if format is not handled.
    """
    # Determine data offset — DX10 header is 20 bytes extra
    # BUG 1 FIX (same offset as above): dwFourCC is at h_off+80, not h_off+76.
    # h_off = 4 (start of DDS_HEADER), ddspf starts at h_off+72, dwFourCC at +8 within ddspf.
    has_dx10 = (len(data) >= 128 and data[4 + 80 : 4 + 84] == _FOURCC_DX10)
    data_off = 148 if has_dx10 else 128

    raw = data[data_off:]

    f = fmt.upper()

    # BC1 / DXT1
    # BUG 3 FIX: texture2ddecoder returns BGRA, not RGBA. Wrap with _bgra_to_rgba.
    if f in ("BC1", "BC1_UNORM", "BC1_UNORM_SRGB"):
        return _bgra_to_rgba(t2d.decode_bc1(raw, width, height), width, height)

    # BC2 / DXT3
    if f in ("BC2", "BC2_UNORM", "BC2_UNORM_SRGB"):
        return _bgra_to_rgba(t2d.decode_bc2(raw, width, height), width, height)

    # BC3 / DXT5
    if f in ("BC3", "BC3_UNORM", "BC3_UNORM_SRGB"):
        return _bgra_to_rgba(t2d.decode_bc3(raw, width, height), width, height)

    # BC4
    if f in ("BC4", "BC4_UNORM"):
        grey = t2d.decode_bc4(raw, width, height)
        # Expand R8 → RGBA8888
        return _r8_to_rgba(grey)

    # BC5
    if f in ("BC5", "BC5_UNORM"):
        rg = t2d.decode_bc5(raw, width, height)
        # Expand RG8 → RGBA8888
        return _rg8_to_rgba(rg)

    # BC6H
    # BUG 3 FIX: same BGRA output convention as BC1/BC3/BC7.
    if f in ("BC6H_UF16", "BC6H_SF16"):
        return _bgra_to_rgba(t2d.decode_bc6(raw, width, height), width, height)

    # BC7
    if f in ("BC7", "BC7_UNORM", "BC7_UNORM_SRGB"):
        return _bgra_to_rgba(t2d.decode_bc7(raw, width, height), width, height)

    # Uncompressed RGBA8
    if f in ("R8G8B8A8_UNORM", "R8G8B8A8_UNORM_SRGB", "RGBA"):
        return raw[: width * height * 4]

    # Uncompressed BGRA8 → convert to RGBA
    if f in ("B8G8R8A8_UNORM", "B8G8R8A8_UNORM_SRGB"):
        return _bgra_to_rgba(raw, width, height)

    return None


def _r8_to_rgba(data: bytes) -> bytes:
    """Expand single-channel R8 to RGBA8888 (grey, grey, grey, 255)."""
    out = bytearray(len(data) * 4)
    for i, v in enumerate(data):
        out[i*4]     = v
        out[i*4 + 1] = v
        out[i*4 + 2] = v
        out[i*4 + 3] = 255
    return bytes(out)


def _rg8_to_rgba(data: bytes) -> bytes:
    """Expand RG8 to RGBA8888 (R, G, 0, 255)."""
    n = len(data) // 2
    out = bytearray(n * 4)
    for i in range(n):
        out[i*4]     = data[i*2]
        out[i*4 + 1] = data[i*2 + 1]
        out[i*4 + 2] = 0
        out[i*4 + 3] = 255
    return bytes(out)


def _bgra_to_rgba(data: bytes, width: int, height: int) -> bytes:
    """Swap B and R channels: BGRA → RGBA."""
    n = width * height
    out = bytearray(n * 4)
    for i in range(n):
        b = data[i*4]
        g = data[i*4 + 1]
        r = data[i*4 + 2]
        a = data[i*4 + 3]
        out[i*4]     = r
        out[i*4 + 1] = g
        out[i*4 + 2] = b
        out[i*4 + 3] = a
    return bytes(out)