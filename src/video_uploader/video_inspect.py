from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

# Graceful-degradation dependency: pre-flight validation is an
# optimization layer, not a hard requirement for the upload pipeline to
# function (DESIGN.md milestone 8's own design notes). If a file can't be
# parsed, every field below comes back None and downstream preflight
# checks simply skip -- VideoFile's fields are already documented as
# optional for exactly this reason (core/types.py).
_EMPTY_RESULT = {
    "duration_seconds": None,
    "codec": None,
    "width": None,
    "height": None,
}

# Reads no more than this from any single metadata box (mvhd/tkhd/hdlr/
# stsd) -- every field we need lives in the first ~100 bytes of each, so
# this is a safety cap against an oddly large box, not a real limit.
_MAX_METADATA_BOX_READ = 512

_CODEC_FOURCC_NAMES = {
    b"avc1": "h264",
    b"avc3": "h264",
    b"hvc1": "hevc",
    b"hev1": "hevc",
}


def inspect_video(path: Path) -> dict:
    """Reads duration/codec/width/height directly out of an MP4/MOV
    file's box structure (ISO/IEC 14496-12) -- no ffprobe or any other
    external tool needed. Only ever reads small, fixed-size metadata
    boxes; large boxes (mdat, the actual encoded frames) are skipped via
    seek, never read into memory, so this stays cheap even on a
    multi-hundred-MB file. Any parsing failure (not an MP4/MOV, malformed
    box structure, truncated file) falls back to _EMPTY_RESULT rather
    than raising -- see the module docstring-equivalent comment above."""
    try:
        with path.open("rb") as fh:
            file_size = path.stat().st_size
            moov = _find_box(fh, 0, file_size, b"moov")
            if moov is None:
                return dict(_EMPTY_RESULT)
            moov_start, moov_end = moov

            duration_seconds = None
            mvhd = _find_box(fh, moov_start, moov_end, b"mvhd")
            if mvhd is not None:
                duration_seconds = _parse_mvhd_duration(_read_box(fh, mvhd))

            width = height = codec = None
            for box_type, trak_start, trak_end in _iter_boxes(fh, moov_start, moov_end):
                if box_type != b"trak":
                    continue
                mdia = _find_box(fh, trak_start, trak_end, b"mdia")
                if mdia is None:
                    continue
                hdlr = _find_box(fh, mdia[0], mdia[1], b"hdlr")
                if hdlr is None or _read_box(fh, hdlr)[8:12] != b"vide":
                    continue  # not the video track (e.g. audio/subtitle)

                tkhd = _find_box(fh, trak_start, trak_end, b"tkhd")
                if tkhd is not None:
                    width, height = _parse_tkhd_dimensions(_read_box(fh, tkhd))

                minf = _find_box(fh, mdia[0], mdia[1], b"minf")
                stbl = _find_box(fh, *minf, b"stbl") if minf else None
                stsd = _find_box(fh, *stbl, b"stsd") if stbl else None
                if stsd is not None:
                    codec = _parse_stsd_codec(_read_box(fh, stsd))
                break  # first video track is all preflight needs
    except (OSError, ValueError, IndexError):
        return dict(_EMPTY_RESULT)

    return {
        "duration_seconds": duration_seconds,
        "codec": codec,
        "width": width,
        "height": height,
    }


def _iter_boxes(fh: BinaryIO, start: int, end: int):
    """Yields (box_type, content_start, content_end) for each top-level
    box in [start, end), seeking past each one's content without reading
    it -- callers read a box's bytes explicitly (via _read_box) only when
    they actually need to."""
    pos = start
    while pos < end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size = int.from_bytes(header[0:4], "big")
        box_type = header[4:8]
        header_len = 8
        if size == 1:
            extended = fh.read(8)
            if len(extended) < 8:
                return
            size = int.from_bytes(extended, "big")
            header_len = 16
        elif size == 0:
            size = end - pos
        if size < header_len:
            return  # malformed -- refuse to loop forever or go backwards
        content_start = pos + header_len
        content_end = pos + size
        yield box_type, content_start, min(content_end, end)
        pos = content_end


def _find_box(fh: BinaryIO, start: int, end: int, box_type: bytes) -> tuple[int, int] | None:
    for bt, content_start, content_end in _iter_boxes(fh, start, end):
        if bt == box_type:
            return content_start, content_end
    return None


def _read_box(fh: BinaryIO, box: tuple[int, int]) -> bytes:
    content_start, content_end = box
    fh.seek(content_start)
    return fh.read(min(content_end - content_start, _MAX_METADATA_BOX_READ))


def _parse_mvhd_duration(data: bytes) -> float | None:
    if not data:
        return None
    version = data[0]
    if version == 1:
        if len(data) < 32:
            return None
        timescale = int.from_bytes(data[20:24], "big")
        duration = int.from_bytes(data[24:32], "big")
    else:
        if len(data) < 20:
            return None
        timescale = int.from_bytes(data[12:16], "big")
        duration = int.from_bytes(data[16:20], "big")
    if timescale == 0:
        return None
    return duration / timescale


def _parse_tkhd_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if not data:
        return None, None
    version = data[0]
    width_offset, min_len = (88, 96) if version == 1 else (76, 84)
    if len(data) < min_len:
        return None, None
    # width/height are 32-bit 16.16 fixed-point -- divide out the 16 low
    # fraction bits to get the plain pixel dimension.
    width = int.from_bytes(data[width_offset : width_offset + 4], "big") / 65536
    height = int.from_bytes(data[width_offset + 4 : width_offset + 8], "big") / 65536
    return int(width), int(height)


def _parse_stsd_codec(data: bytes) -> str | None:
    if len(data) < 16:
        return None
    entry_count = int.from_bytes(data[4:8], "big")
    if entry_count < 1:
        return None
    fourcc = data[12:16]
    if fourcc in _CODEC_FOURCC_NAMES:
        return _CODEC_FOURCC_NAMES[fourcc]
    try:
        return fourcc.decode("ascii").strip().lower() or None
    except UnicodeDecodeError:
        return None
