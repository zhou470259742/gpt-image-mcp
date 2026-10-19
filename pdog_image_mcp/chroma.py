"""Dynamic chroma-key selection and edge-connected background extraction.

Carried over from the skill's chroma_key.py. Requires opencv-python-headless
and numpy (install with `uv sync --extra chroma`); the import is guarded so the
rest of the engine works without them and the chroma tools fail with a clear
message instead of breaking server startup.
"""

from __future__ import annotations

import io
from typing import Iterable, Tuple

from .errors import PdogError

try:
    import cv2
    import numpy as np
    from PIL import Image, UnidentifiedImageError

    AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    UnidentifiedImageError = Exception  # type: ignore[assignment,misc]
    AVAILABLE = False

RGB = Tuple[int, int, int]
KEY_CANDIDATES: Tuple[RGB, ...] = (
    (255, 0, 255),   # magenta
    (0, 255, 0),     # green
    (255, 0, 0),     # red
    (0, 255, 255),   # cyan
    (255, 255, 0),   # yellow
    (255, 96, 0),    # orange
    (127, 0, 255),   # violet
)


def _require_available() -> None:
    if not AVAILABLE:
        raise PdogError(
            "色键模块需要 opencv-python-headless 与 numpy（当前环境未安装）。",
            kind="missing_dependency",
            hint="在项目目录执行 uv sync --extra chroma，或改用原生透明路线。",
        )


def parse_hex_color(value: str) -> RGB:
    raw = value.strip().lstrip("#")
    if len(raw) != 6:
        raise ValueError("Color must use six hexadecimal digits, for example #FF00FF.")
    try:
        return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as exc:
        raise ValueError("Color must use six hexadecimal digits, for example #FF00FF.") from exc


def color_hex(color: RGB) -> str:
    return "#" + "".join(f"{channel:02X}" for channel in color)


def load_rgba(image_bytes: bytes) -> "Image.Image":
    _require_available()
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Input is not a readable raster image.") from exc
    return image.convert("RGBA")


def choose_key_color(
    image_bytes: bytes, candidates: Iterable[RGB] = KEY_CANDIDATES
) -> Tuple[RGB, dict]:
    """Choose the candidate least likely to collide with visible source pixels."""
    _require_available()
    image = load_rgba(image_bytes)
    image.thumbnail((512, 512), Image.Resampling.LANCZOS)
    rgba = np.asarray(image, dtype=np.float32)
    visible = rgba[:, :, 3] > 16
    pixels = rgba[:, :, :3][visible]
    if not len(pixels):
        selected = KEY_CANDIDATES[0]
        return selected, {"selected": color_hex(selected), "scores": {color_hex(selected): 0.0}}

    # Perceptual-ish weighting: human vision is most sensitive to green differences.
    weights = np.array([0.30, 0.59, 0.11], dtype=np.float32)
    scores = {}
    for candidate in candidates:
        delta = (pixels - np.array(candidate, dtype=np.float32)) * np.sqrt(weights)
        distance = np.linalg.norm(delta, axis=1)
        collision = float(np.mean(distance < 54.0))
        low_percentile = float(np.percentile(distance, 1.0))
        # Prefer separation across the palette and heavily penalize actual near-matches.
        scores[color_hex(candidate)] = round(low_percentile - collision * 1000.0, 4)

    selected_hex = max(scores, key=scores.get)
    selected = parse_hex_color(selected_hex)
    return selected, {"selected": selected_hex, "scores": scores}


def extract_edge_connected_key(
    image_bytes: bytes,
    key_color: RGB,
    tolerance: float = 30.0,
    feather: float = 40.0,
    despill_distance: float = 55.0,
    despill_radius: int = 6,
) -> Tuple[bytes, dict]:
    """Convert only edge-connected key-colored pixels to alpha and decontaminate fringes."""
    _require_available()
    image = load_rgba(image_bytes)
    rgba = np.asarray(image, dtype=np.uint8)
    rgb = rgba[:, :, :3]

    rgb_lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    key_sample = np.array([[key_color]], dtype=np.uint8)
    key_lab = cv2.cvtColor(key_sample, cv2.COLOR_RGB2LAB).astype(np.float32)[0, 0]
    distance = np.linalg.norm(rgb_lab - key_lab, axis=2)
    outer = tolerance + feather
    candidate = (distance <= outer).astype(np.uint8)

    count, labels = cv2.connectedComponents(candidate, connectivity=8)
    if count <= 1:
        raise ValueError(f"No background pixels matched dynamic key color {color_hex(key_color)}.")
    border_labels = np.unique(np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1])))
    border_labels = border_labels[border_labels != 0]
    connected = np.isin(labels, border_labels)
    if not np.any(connected):
        raise ValueError(f"No edge-connected background matched dynamic key color {color_hex(key_color)}.")

    alpha = np.ones(distance.shape, dtype=np.float32)
    keyed_alpha = np.clip((distance - tolerance) / max(feather, 1.0), 0.0, 1.0)
    alpha[connected] = keyed_alpha[connected]

    # Remove key-color spill from both the semi-transparent antialiasing and a
    # narrow opaque boundary band. Image models often paint one or two fully
    # opaque key-coloured pixels around a subject; limiting despill to alpha
    # pixels alone leaves a visible green/magenta halo on dark backgrounds.
    # The spatial band prevents similarly coloured details inside the subject
    # from being changed.
    output_rgb = rgb.astype(np.float32)
    partial = connected & (alpha > 0.001) & (alpha < 0.999)
    radius = max(int(despill_radius), 0)
    if radius:
        distance_to_background = cv2.distanceTransform(
            (~connected).astype(np.uint8),
            cv2.DIST_L2,
            cv2.DIST_MASK_PRECISE,
        )
        near_background = distance_to_background <= float(radius)
    else:
        near_background = connected
    spill_limit = outer + max(float(despill_distance), 0.0)
    opaque_spill = near_background & (alpha >= 0.999) & (distance <= spill_limit)
    replace = partial | opaque_spill
    if np.any(replace):
        reliable_foreground = (alpha >= 0.999) & ~opaque_spill
        if not np.any(reliable_foreground):
            raise ValueError("Chroma-key despill could not find reliable foreground pixels.")
        inverse_foreground = (~reliable_foreground).astype(np.uint8)
        _, nearest = cv2.distanceTransformWithLabels(
            inverse_foreground,
            cv2.DIST_L2,
            5,
            labelType=cv2.DIST_LABEL_PIXEL,
        )
        foreground_colors = rgb[reliable_foreground]
        labels = nearest[replace] - 1
        valid = (labels >= 0) & (labels < len(foreground_colors))
        replacement_colors = output_rgb[replace]
        replacement_colors[valid] = foreground_colors[labels[valid]]
        output_rgb[replace] = replacement_colors
    output_rgb = np.clip(output_rgb, 0, 255).astype(np.uint8)
    output_rgb[alpha <= 0.001] = 0
    output_alpha = np.round(alpha * 255.0).astype(np.uint8)
    output = np.dstack((output_rgb, output_alpha))

    total = int(output_alpha.size)
    transparent = int(np.count_nonzero(output_alpha == 0))
    partial_count = int(np.count_nonzero((output_alpha > 0) & (output_alpha < 255)))
    opaque = total - transparent - partial_count
    if transparent == 0:
        raise ValueError("Chroma-key extraction produced no transparent pixels.")
    if opaque == 0:
        raise ValueError("Chroma-key extraction removed the entire image.")

    buffer = io.BytesIO()
    Image.fromarray(output).save(buffer, format="PNG")
    return buffer.getvalue(), {
        "key_color": color_hex(key_color),
        "size": [image.width, image.height],
        "transparent_pixels": transparent,
        "partially_transparent_pixels": partial_count,
        "opaque_pixels": opaque,
        "transparent_percent": round(transparent * 100.0 / total, 2),
        "tolerance_lab": tolerance,
        "feather_lab": feather,
        "despill_distance_lab": despill_distance,
        "despill_radius_px": radius,
        "opaque_despill_pixels": int(np.count_nonzero(opaque_spill)),
    }
