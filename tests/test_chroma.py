"""Unit tests for the chroma-key module (requires the chroma extra)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from pdog_image_mcp import chroma, core
from pdog_image_mcp.core import PdogError, chroma_key_file
from tests.helpers import keyed_png

pytestmark = pytest.mark.skipif(not chroma.AVAILABLE, reason="chroma extra not installed")


def test_choose_key_color_avoids_magenta_background():
    image = keyed_png()  # magenta background + green subject
    color, details = chroma.choose_key_color(image)
    assert details["scores"]
    # magenta covers ~90% of visible pixels, so it must not be selected
    assert color != (255, 0, 255)


def test_extract_edge_connected_key_no_resize_and_alpha():
    output, metrics = chroma.extract_edge_connected_key(keyed_png(), (255, 0, 255))
    assert metrics["key_color"] == "#FF00FF"
    assert metrics["size"] == [64, 64]
    assert metrics["transparent_percent"] > 50
    image = Image.open(io.BytesIO(output))
    assert image.size == (64, 64)
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((32, 32))[3] == 255


def test_extract_rejects_color_not_present():
    with pytest.raises(ValueError):
        chroma.extract_edge_connected_key(keyed_png(), (0, 0, 255))


def test_standalone_chroma_key_file():
    out, metrics = chroma_key_file(keyed_png(), key_color="#FF00FF")
    assert out.startswith(b"\x89PNG")
    assert metrics["transparent_percent"] > 50
    with pytest.raises(PdogError):
        chroma_key_file(keyed_png(), key_color="red")


def test_chroma_key_file_validates_params():
    with pytest.raises(PdogError):
        chroma_key_file(keyed_png(), tolerance=-1)
    with pytest.raises(PdogError):
        chroma_key_file(keyed_png(), feather=0)
