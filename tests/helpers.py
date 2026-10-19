"""Shared helpers for tests: synthetic PNG fixtures and a fake engine config."""

from __future__ import annotations

import base64
import io

from PIL import Image

FAKE_CONFIG = {
    "base_url": "https://api.example.test",
    "model": "gpt-image-2.5-flare",
    "models": {
        "flare": "gpt-image-2.5-flare",
        "sunburst": "gpt-image-2.5-sunburst",
        "image-2": "gpt-image-2",
    },
    "model_default_alias": "flare",
    "model_auto_routing": {"generation": "flare", "edit": "sunburst", "edit_4k": "flare"},
    "async": {"mode": "auto", "poll_interval_seconds": 5, "task_timeout_seconds": 1800},
    "default_resolution": "1k",
    "default_quality": "xhigh",
    "timeout_seconds": 300,
    "api_keys": {
        "1k": "sk-test-regular-key-000000000000000000",
        "4k": "sk-test-4k-key-1111111111111111111",
    },
    "api_key": "sk-legacy-fallback-key",
}


def png_bytes(
    size: tuple[int, int] = (8, 8),
    fill: tuple[int, ...] = (255, 0, 0, 255),
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, fill).save(buffer, format="PNG")
    return buffer.getvalue()


def png_with_transparent_margin(
    size: tuple[int, int] = (20, 20),
    box: tuple[int, int, int, int] = (5, 5, 15, 15),
    color: tuple[int, ...] = (200, 30, 30, 255),
) -> bytes:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste(color, box)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def png_with_soft_edge(
    size: tuple[int, int] = (20, 20),
    border_alpha: int = 5,
) -> bytes:
    """Opaque center square touching the border only with low-alpha pixels."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((10, 10, 10, 255), (5, 5, 15, 15))
    pixels = image.load()
    for x in range(size[0]):
        for y in range(size[1]):
            if x == 0 or y == 0 or x == size[0] - 1 or y == size[1] - 1:
                r, g, b, _ = pixels[x, y]
                pixels[x, y] = (r, g, b, border_alpha)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def keyed_png() -> bytes:
    """64x64 magenta background with a centered 20x20 opaque green square."""
    image = Image.new("RGBA", (64, 64), (255, 0, 255, 255))
    image.paste((0, 200, 0, 255), (22, 22, 42, 42))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def openai_b64_response(image: bytes) -> bytes:
    return (
        json_dumps({"data": [{"b64_json": base64.b64encode(image).decode()}]})
    )


def json_dumps(payload) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")
