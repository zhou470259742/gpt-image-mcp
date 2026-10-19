"""Unit tests for transparency verification (Pillow-only implementation)."""

from __future__ import annotations

import pytest

from pdog_image_mcp import transparency
from tests.helpers import png_bytes, png_with_soft_edge, png_with_transparent_margin


def test_has_real_transparency():
    assert transparency.has_real_transparency(png_with_transparent_margin())
    assert not transparency.has_real_transparency(png_bytes((4, 4)))


def test_verify_opaque_image():
    report = transparency.verify_transparency(png_bytes((10, 10)))
    assert report["verdict"] == "opaque"
    assert report["has_alpha"] is False
    # alpha>=8 everywhere -> bbox spans the full canvas
    assert report["meaningful_bbox"] == [0, 0, 10, 10]


def test_verify_pass_with_margins():
    report = transparency.verify_transparency(png_with_transparent_margin())
    assert report["verdict"] == "pass"
    assert report["meaningful_bbox"] == [5, 5, 15, 15]
    assert report["margins_px"] == {"left": 5, "top": 5, "right": 5, "bottom": 5}
    assert report["border"]["softEdgeContact"] is False
    assert report["pixels"]["fully_transparent"] == 400 - 100


def test_verify_fail_on_substantial_edge_contact():
    # opaque top half over a transparent bottom half: the substantive outline
    # touches the top/left/right edges -> fail per the skill's boundary rules
    image = png_with_transparent_margin(size=(20, 20), box=(0, 0, 20, 10))
    report = transparency.verify_transparency(image)
    assert report["verdict"] == "fail"
    assert report["border"]["substantial_contact_pixels"] > 0
    assert set(report["border"]["edges_with_substantial_contact"]) == {"top", "left", "right"}


def test_verify_review_on_soft_edge_contact():
    report = transparency.verify_transparency(png_with_soft_edge())
    assert report["verdict"] == "review"
    assert report["border"]["softEdgeContact"] is True
    assert report["border"]["soft_contact_pixels"] > 0
    assert report["border"]["max_border_alpha"] == 5
    assert report["border"]["substantial_contact_pixels"] == 0


def test_verify_rejects_non_image():
    with pytest.raises(ValueError):
        transparency.verify_transparency(b"definitely not a png")
