"""PNG transparency verification, aligned with the skill's canvas-boundary rules.

Pillow-only (no numpy/cv2) so the main engine path stays dependency-light.

Rules carried over from the skill (v2.1, canvas-boundary verification):
- meaningful BBox is computed at alpha >= 8 (alpha 1-7 is treated as noise);
- substantive outline edge contact is checked at alpha >= 16; that is a failure;
- border pixels with alpha 1-15 are reported as `softEdgeContact` (antialiasing
  or glow tails) and go to manual review on checkerboard/white/black backgrounds
  instead of automatically failing.
"""

from __future__ import annotations

import io
from typing import Any

from PIL import Image, UnidentifiedImageError

MEANINGFUL_ALPHA = 8
SUBSTANTIAL_ALPHA = 16


def has_real_transparency(image_bytes: bytes) -> bool:
    """True when the image carries at least one non-opaque pixel."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (UnidentifiedImageError, OSError):
        return False
    alpha = image.convert("RGBA").getchannel("A")
    return alpha.getextrema()[0] < 255


def _border_strips(alpha: Image.Image, width: int, height: int) -> dict[str, Image.Image]:
    return {
        "top": alpha.crop((0, 0, width, 1)),
        "bottom": alpha.crop((0, height - 1, width, height)),
        "left": alpha.crop((0, 0, 1, height)),
        "right": alpha.crop((width - 1, 0, width, height)),
    }


def verify_transparency(
    image_bytes: bytes,
    *,
    meaningful_alpha: int = MEANINGFUL_ALPHA,
    substantial_alpha: int = SUBSTANTIAL_ALPHA,
) -> dict[str, Any]:
    """Static transparency report for one PNG (or any raster image)."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"Input is not a readable raster image: {exc}") from exc

    rgba = image.convert("RGBA")
    width, height = rgba.size
    alpha = rgba.getchannel("A")
    hist = alpha.histogram()
    total = width * height
    fully_transparent = hist[0]
    partially = sum(hist[1:255])
    opaque = hist[255]
    has_alpha = (fully_transparent + partially) > 0

    meaningful_mask = alpha.point(lambda a: 255 if a >= meaningful_alpha else 0)
    bbox = meaningful_mask.getbbox()
    margins = None
    if bbox:
        margins = {
            "left": bbox[0],
            "top": bbox[1],
            "right": width - bbox[2],
            "bottom": height - bbox[3],
        }

    border_pixels = 2 * (width + height)
    border_substantial = 0
    border_soft = 0
    border_max_alpha = 0
    edges_with_substantial: list[str] = []
    edges_with_soft: list[str] = []
    for name, strip in _border_strips(alpha, width, height).items():
        strip_hist = strip.histogram()
        substantial = sum(strip_hist[substantial_alpha:])
        soft = sum(strip_hist[1:substantial_alpha])
        border_substantial += substantial
        border_soft += soft
        edge_max = max((value for value, count in enumerate(strip_hist) if count), default=0)
        border_max_alpha = max(border_max_alpha, edge_max)
        if substantial:
            edges_with_substantial.append(name)
        if soft:
            edges_with_soft.append(name)

    if not has_alpha:
        verdict = "opaque"
        detail = "图像没有任何透明像素（全部 alpha=255），不是透明素材。"
    elif border_substantial > 0:
        verdict = "fail"
        detail = (
            f"alpha>={substantial_alpha} 的实质轮廓接触画布边缘"
            f"（{', '.join(edges_with_substantial)}），判定裁切失败。"
        )
    elif border_soft > 0:
        verdict = "review"
        detail = (
            f"边缘仅有低 Alpha（1~{substantial_alpha - 1}）抗锯齿/发光尾部"
            f"（softEdgeContact，{'、'.join(edges_with_soft)}），"
            "需人工在棋盘格/白底/黑底上复核后才可交付。"
        )
    else:
        verdict = "pass"
        detail = "实质轮廓未接触画布边缘，留白充分。"

    return {
        "size": [width, height],
        "has_alpha": has_alpha,
        "pixels": {
            "fully_transparent": fully_transparent,
            "partially_transparent": partially,
            "opaque": opaque,
            "fully_transparent_percent": round(fully_transparent * 100.0 / total, 2),
        },
        "meaningful_bbox": list(bbox) if bbox else None,
        "margins_px": margins,
        "border": {
            "substantial_contact_pixels": border_substantial,
            "soft_contact_pixels": border_soft,
            "soft_contact_ratio": round(border_soft * 1.0 / border_pixels, 6) if border_pixels else 0.0,
            "max_border_alpha": border_max_alpha,
            "softEdgeContact": border_soft > 0,
            "edges_with_substantial_contact": edges_with_substantial,
            "edges_with_soft_contact": edges_with_soft,
        },
        "verdict": verdict,
        "detail": detail,
    }
