import subprocess

import pytest

from video_uploader import video_inspect


@pytest.fixture(scope="module")
def real_clip(tmp_path_factory):
    dest = tmp_path_factory.mktemp("video_inspect") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def test_inspect_video_returns_real_data(real_clip):
    result = video_inspect.inspect_video(real_clip)
    assert result["duration_seconds"] == pytest.approx(2.0, abs=0.5)
    assert result["codec"] == "h264"
    assert result["width"] == 320
    assert result["height"] == 240


def test_inspect_video_returns_none_fields_for_missing_file(tmp_path):
    result = video_inspect.inspect_video(tmp_path / "does-not-exist.mp4")
    assert result == {
        "duration_seconds": None,
        "codec": None,
        "width": None,
        "height": None,
    }


def test_inspect_video_returns_none_fields_for_garbage_file(tmp_path):
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"not a real video file")
    result = video_inspect.inspect_video(garbage)
    assert result["duration_seconds"] is None
    assert result["codec"] is None


def test_inspect_video_returns_none_fields_when_no_moov_box(tmp_path):
    # A syntactically valid box stream (so _iter_boxes doesn't bail
    # immediately like it does on pure garbage) that never contains moov.
    no_moov = tmp_path / "no-moov.mp4"
    no_moov.write_bytes(b"\x00\x00\x00\x08ftyp")
    result = video_inspect.inspect_video(no_moov)
    assert result == {
        "duration_seconds": None,
        "codec": None,
        "width": None,
        "height": None,
    }


def _box(box_type: bytes, content: bytes) -> bytes:
    return (8 + len(content)).to_bytes(4, "big") + box_type + content


def test_parse_mvhd_duration_version_0():
    data = b"\x00" + b"\x00" * 3 + b"\x00" * 8 + (1000).to_bytes(4, "big") + (5000).to_bytes(4, "big")
    assert video_inspect._parse_mvhd_duration(data) == pytest.approx(5.0)


def test_parse_mvhd_duration_version_1_64bit():
    data = (
        b"\x01" + b"\x00" * 3 + b"\x00" * 16 + (1000).to_bytes(4, "big") + (5000).to_bytes(8, "big")
    )
    assert video_inspect._parse_mvhd_duration(data) == pytest.approx(5.0)


def test_parse_mvhd_duration_handles_zero_timescale():
    data = b"\x00" + b"\x00" * 3 + b"\x00" * 8 + (0).to_bytes(4, "big") + (5000).to_bytes(4, "big")
    assert video_inspect._parse_mvhd_duration(data) is None


def test_parse_tkhd_dimensions_version_0():
    data = bytearray(84)
    data[76:80] = (1080 << 16).to_bytes(4, "big")
    data[80:84] = (1920 << 16).to_bytes(4, "big")
    assert video_inspect._parse_tkhd_dimensions(bytes(data)) == (1080, 1920)


def test_parse_tkhd_dimensions_version_1():
    data = bytearray(96)
    data[0] = 1
    data[88:92] = (640 << 16).to_bytes(4, "big")
    data[92:96] = (480 << 16).to_bytes(4, "big")
    assert video_inspect._parse_tkhd_dimensions(bytes(data)) == (640, 480)


@pytest.mark.parametrize(
    "fourcc,expected",
    [(b"avc1", "h264"), (b"avc3", "h264"), (b"hvc1", "hevc"), (b"hev1", "hevc"), (b"vp09", "vp09")],
)
def test_parse_stsd_codec_maps_known_fourccs(fourcc, expected):
    data = b"\x00\x00\x00\x00" + (1).to_bytes(4, "big") + (16).to_bytes(4, "big") + fourcc
    assert video_inspect._parse_stsd_codec(data) == expected


def test_parse_stsd_codec_returns_none_for_zero_entries():
    data = b"\x00\x00\x00\x00" + (0).to_bytes(4, "big") + b"\x00" * 8
    assert video_inspect._parse_stsd_codec(data) is None


def test_inspect_video_ignores_audio_only_track(tmp_path):
    """A trak whose handler_type is 'soun' (audio), not 'vide', must not
    contribute codec/width/height -- those should stay None even though
    duration (from mvhd, at the moov level) is still populated."""
    mvhd = _box(
        b"mvhd",
        b"\x00" + b"\x00" * 3 + b"\x00" * 8 + (1000).to_bytes(4, "big") + (3000).to_bytes(4, "big"),
    )
    hdlr = _box(b"hdlr", b"\x00" * 8 + b"soun" + b"\x00" * 12)
    mdia = _box(b"mdia", hdlr)
    trak = _box(b"trak", mdia)
    moov = _box(b"moov", mvhd + trak)
    ftyp = _box(b"ftyp", b"isommp42")

    audio_only = tmp_path / "audio-only.mp4"
    audio_only.write_bytes(ftyp + moov)

    result = video_inspect.inspect_video(audio_only)
    assert result["duration_seconds"] == pytest.approx(3.0)
    assert result["codec"] is None
    assert result["width"] is None
    assert result["height"] is None
