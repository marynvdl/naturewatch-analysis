"""Checks for the version 3 transcode. They need GDAL and no network.

Run: python -m pytest tiles
"""
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
from osgeo import gdal

sys.path.insert(0, str(Path(__file__).parent))

import cropland  # noqa: E402
import pyramid  # noqa: E402
import transcode as tc  # noqa: E402
from pmtiles.tile import zxy_to_tileid  # noqa: E402

NODATA = tc.NODATA
V3 = [name for name in tc.ENCODINGS if name != "gray-alpha"]


def tiles() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(3)
    mixed = (rng.random((256, 256)) < 0.2).astype(np.uint8)
    partly = mixed.copy()
    partly[:, :90] = NODATA
    return {
        "all-zero": np.zeros((256, 256), np.uint8),
        "all-one": np.ones((256, 256), np.uint8),
        "mixed": mixed,
        "partly-nodata": partly,
        "one-pixel": np.pad(np.ones((1, 1), np.uint8), ((5, 250), (7, 248))),
    }


def colour_type(data: bytes) -> tuple[int, int]:
    return data[24], data[25]


@pytest.mark.parametrize("encoding", sorted(tc.ENCODINGS))
@pytest.mark.parametrize("name", sorted(tiles()))
def test_every_encoding_keeps_class_and_nodata(encoding, name):
    values = tiles()[name]
    data = tc.ENCODINGS[encoding](values)
    assert np.array_equal(tc.decode_values(data, encoding), values)
    assert np.array_equal(tc.gdal_values(data, encoding), values)


@pytest.mark.parametrize("encoding", V3)
def test_no_version_3_encoding_is_gray(encoding):
    for values in tiles().values():
        _depth, kind = colour_type(tc.ENCODINGS[encoding](values))
        assert kind in (2, 3, 6)


def test_gray_alpha_is_the_version_2_encoder():
    for values in tiles().values():
        assert tc.encode_gray_alpha(values) == cropland.tile_png(values)


def test_palette_uses_one_bit_without_nodata_and_two_with():
    assert colour_type(tc.ENCODINGS["palette-r1"](tiles()["mixed"])) == (1, 3)
    assert colour_type(tc.ENCODINGS["palette-r1"](tiles()["partly-nodata"])) == (2, 3)


def test_rgb_marks_nodata_with_trns_only_when_needed():
    assert b"tRNS" not in tc.ENCODINGS["rgb-r1"](tiles()["mixed"])
    assert b"tRNS" in tc.ENCODINGS["rgb-r1"](tiles()["partly-nodata"])


def test_red_channel_carries_the_class():
    rgba = tc.decode_rgba(tc.ENCODINGS["palette-r255"](tiles()["partly-nodata"]))
    values = tiles()["partly-nodata"]
    assert set(np.unique(rgba[values == 1][:, 0])) == {255}
    assert set(np.unique(rgba[values == NODATA][:, 3])) == {0}
    assert set(np.unique(tc.decode_rgba(tc.ENCODINGS["rgb-r1"](values))[values == 1][:, 0])) == {1}


def test_encoders_refuse_classes_they_cannot_hold():
    values = np.full((256, 256), 2, np.uint8)
    with pytest.raises(ValueError):
        tc.ENCODINGS["palette-r1"](values)


def test_decode_v2_refuses_anything_but_gray_alpha():
    with pytest.raises(ValueError):
        tc.decode_v2(tc.ENCODINGS["rgba-r1"](tiles()["mixed"]))
    gray = np.zeros((256, 256), np.uint8)
    gray[0, 0] = 1
    alpha = np.zeros((256, 256), np.uint8)
    with pytest.raises(ValueError, match="nodata would lose"):
        tc.decode_v2(cropland.encode_class_png(gray, alpha))


def test_decoder_reads_filtered_pngs_like_gdal(tmp_path):
    rng = np.random.default_rng(1)
    pixels = (rng.random((3, 64, 64)) * 255).astype(np.uint8)
    pixels[:, 10:30] = 7
    source = gdal.GetDriverByName("MEM").Create("", 64, 64, 3, gdal.GDT_Byte)
    for band in range(3):
        source.GetRasterBand(band + 1).WriteArray(pixels[band])
    path = tmp_path / "filtered.png"
    gdal.GetDriverByName("PNG").CreateCopy(str(path), source)
    data = path.read_bytes()
    assert any(kind != 0 for kind in _filter_types(data, 64, 3)), "GDAL wrote only filter 0, so this test checks nothing"
    assert np.array_equal(tc.decode_rgba(data)[..., :3], np.moveaxis(pixels, 0, -1))


def _filter_types(data: bytes, width: int, channels: int) -> set[int]:
    import zlib

    position, idat = 8, b""
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        if data[position + 4 : position + 8] == b"IDAT":
            idat += data[position + 8 : position + 8 + length]
        position += 12 + length
    raw = zlib.decompress(idat)
    stride = width * channels + 1
    return {raw[i] for i in range(0, len(raw), stride)}


def v2_archive(path: Path) -> dict[int, np.ndarray]:
    """A small archive in the version 2 encoding: zooms 0 to 3, with a repeated tile and a missing one."""
    rng = np.random.default_rng(7)
    arrays = {}
    for z in range(4):
        for x in range(2**z):
            for y in range(2**z):
                if (z, x, y) == (3, 7, 7):
                    continue
                kind = (x + y + z) % 4
                if kind == 0:
                    values = np.zeros((256, 256), np.uint8)
                elif kind == 1:
                    values = (rng.random((256, 256)) < 0.3).astype(np.uint8)
                elif kind == 2:
                    values = np.zeros((256, 256), np.uint8)
                    values[:, 200:] = NODATA
                    values[:40, :40] = 1
                else:
                    values = np.ones((256, 256), np.uint8)
                arrays[zxy_to_tileid(z, x, y)] = values
    entries = [(tile_id, cropland.tile_png(values)) for tile_id, values in sorted(arrays.items())]
    pyramid.write_archive(entries, path, pyramid.archive_header((-180, -85, 180, 85), 0, 3), {"encoding": "class-value", "version": "2"})
    return arrays


@pytest.mark.parametrize("encoding", ["palette-r1", "rgb-r1", "rgba-r255"])
def test_transcoded_archive_matches_version_2(tmp_path, encoding):
    arrays = v2_archive(tmp_path / "v2.pmtiles")
    result = tc.command_transcode(tmp_path / "v2.pmtiles", tmp_path / "v3.pmtiles", encoding, "3")
    assert result["tiles"] == len(arrays)
    check = tc.compare(tmp_path / "v2.pmtiles", tmp_path / "v3.pmtiles", encoding)
    assert (check["checked"], check["differ"], check["missing"], check["extra"]) == (len(arrays), 0, 0, 0)
    assert all(check["header_fields_equal"].values())
    get = tc.file_source(tmp_path / "v3.pmtiles")
    metadata = tc.read_metadata(get, tc.read_header(get))
    assert metadata["encoding"] == f"class-value-{encoding}"
    assert metadata["version"] == "3"
    for tile_id, data in tc.tiles_in_order(get):
        assert np.array_equal(tc.decode_values(data, encoding), arrays[tile_id])


def test_compare_finds_a_changed_pixel_and_a_missing_tile(tmp_path):
    arrays = v2_archive(tmp_path / "v2.pmtiles")
    changed = dict(arrays)
    first = min(changed)
    changed[first] = changed[first].copy()
    changed[first][0, 0] = 1 - changed[first][0, 0]
    del changed[max(changed)]
    entries = [(tile_id, tc.ENCODINGS["palette-r1"](values)) for tile_id, values in sorted(changed.items())]
    pyramid.write_archive(entries, tmp_path / "bad.pmtiles", pyramid.archive_header((-180, -85, 180, 85), 0, 3), {})
    check = tc.compare(tmp_path / "v2.pmtiles", tmp_path / "bad.pmtiles", "palette-r1")
    assert check["differ"] == 1
    assert check["missing"] == 1


def test_512_tile_is_its_four_children(tmp_path):
    arrays = v2_archive(tmp_path / "v2.pmtiles")
    mosaics = dict(tc.mosaics(tc.file_source(tmp_path / "v2.pmtiles")))
    parent = mosaics[zxy_to_tileid(2, 3, 3)]
    assert parent.shape == (512, 512)
    assert np.array_equal(parent[:256, :256], arrays[zxy_to_tileid(3, 6, 6)])
    assert np.array_equal(parent[:256, 256:], arrays[zxy_to_tileid(3, 7, 6)])
    assert np.array_equal(parent[256:, :256], arrays[zxy_to_tileid(3, 6, 7)])
    assert (parent[256:, 256:] == NODATA).all()
    assert zxy_to_tileid(3, 0, 0) not in mosaics
    assert len(mosaics) == 1 + 4 + 16


def test_512_archive_round_trips(tmp_path):
    v2_archive(tmp_path / "v2.pmtiles")
    result = tc.command_transcode(tmp_path / "v2.pmtiles", tmp_path / "v3.pmtiles", "palette-r1", "3", tile_size=512)
    get = tc.file_source(tmp_path / "v3.pmtiles")
    header = tc.read_header(get)
    assert result["tiles"] == 21
    assert header["max_zoom"] == 2
    expected = dict(tc.mosaics(tc.file_source(tmp_path / "v2.pmtiles")))
    for tile_id, data in tc.tiles_in_order(get):
        assert np.array_equal(tc.gdal_values(data, "palette-r1"), expected[tile_id])
