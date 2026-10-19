"""Unit tests for the engine: routing, guards, masking, multipart, pipelines."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from PIL import Image

from pdog_image_mcp import core
from pdog_image_mcp.core import (
    GenRequest,
    PdogError,
    encode_multipart,
    get_async_endpoint,
    get_endpoint,
    load_config,
    mask_key,
    png_dimensions,
    resolve_image_input,
    resolve_model,
    resolve_tier,
    response_message,
    run_generation,
)
from tests.helpers import FAKE_CONFIG, keyed_png, png_bytes, png_with_transparent_margin


# --- masking & error messages -------------------------------------------------

def test_mask_key_short_and_long():
    assert mask_key("short") == "****"
    assert mask_key("abcdefghijkl") == "****"  # len 12 is still fully masked
    masked = mask_key("abcdefghijklm")
    assert masked.startswith("abcde") and masked.endswith("jklm") and "..." in masked


def test_response_message_variants():
    body = json.dumps({"error": {"message": "bad size"}}).encode()
    assert response_message(body) == "bad size"
    assert response_message(json.dumps({"detail": "nope"}).encode()) == "nope"
    assert response_message(b"not json at all") == "not json at all"


# --- endpoints & png header ---------------------------------------------------

def test_get_endpoint_variants():
    assert get_endpoint("https://x.test").endswith("/v1/images/generations")
    assert get_endpoint("https://x.test/v1", edit_mode=True).endswith("/v1/images/edits")
    assert get_async_endpoint("https://x.test").endswith("/v1/images/generations/async")


def test_png_dimensions():
    raw = png_bytes((13, 7))
    assert png_dimensions(raw) == (13, 7)
    assert png_dimensions(b"not a png") is None


# --- multipart ------------------------------------------------------------------

def test_encode_multipart_roundtrip():
    body, content_type = encode_multipart(
        {"model": "m", "prompt": "hi"},
        [("a.png", "image/png", b"PNGDATA")],
        ("mask.png", "image/png", b"MASKDATA"),
    )
    boundary = content_type.split("boundary=")[1]
    assert body.count(b'name="image"') == 1
    assert b'filename="a.png"' in body and b"PNGDATA" in body
    assert b'name="mask"' in body and b"MASKDATA" in body
    assert body.count(f"--{boundary}".encode()) == 5  # 2 fields + image + mask + closing


# --- tier / key routing ---------------------------------------------------------

def test_tier_defaults_to_1k_regular_key():
    tier, key, size = resolve_tier(FAKE_CONFIG)
    assert (tier, size) == ("1k", "2048x1152")
    assert key == FAKE_CONFIG["api_keys"]["1k"]


def test_tier_4k_explicit():
    tier, key, size = resolve_tier(FAKE_CONFIG, is_4k=True)
    assert (tier, size) == ("4k", "3840x2160")
    assert key == FAKE_CONFIG["api_keys"]["4k"]


def test_tier_2k_uses_regular_key():
    tier, key, size = resolve_tier(FAKE_CONFIG, resolution="2k")
    assert tier == "2k" and size == "2048x1152"
    assert key == FAKE_CONFIG["api_keys"]["1k"]


@pytest.mark.parametrize(
    "size,tier",
    [("1024x1024", "1k"), ("1536x1024", "1k"), ("2048x1360", "2k"),
     ("3840x2160", "4k"), ("4096x4096", "4k"), ("auto", "1k")],
)
def test_tier_inferred_from_size(size, tier):
    resolved, _, _ = resolve_tier(FAKE_CONFIG, size=size)
    assert resolved == tier


def test_tier_rejects_invalid_size_and_resolution():
    with pytest.raises(PdogError) as exc:
        resolve_tier(FAKE_CONFIG, size="123x456")
    assert exc.value.kind == "invalid_parameter"
    with pytest.raises(PdogError):
        resolve_tier(FAKE_CONFIG, resolution="8k")


def test_pseudo_4k_guard():
    with pytest.raises(PdogError) as exc:
        resolve_tier(FAKE_CONFIG, is_4k=True, size="1024x1024")
    assert exc.value.kind == "cost_guard"


def test_missing_key_for_tier():
    config = dict(FAKE_CONFIG, api_keys={}, api_key="")
    with pytest.raises(PdogError) as exc:
        resolve_tier(config, is_4k=True)
    assert exc.value.kind == "config"


# --- model routing ----------------------------------------------------------------

def test_model_auto_generation_flare():
    model, reason = resolve_model(FAKE_CONFIG, edit_mode=False, tier="1k")
    assert model == "gpt-image-2.5-flare" and reason == "auto-generation"


def test_model_auto_edit_sunburst():
    model, reason = resolve_model(FAKE_CONFIG, edit_mode=True, tier="1k")
    assert model == "gpt-image-2.5-sunburst" and reason == "auto-edit"


def test_model_4k_forces_flare():
    model, reason = resolve_model(FAKE_CONFIG, edit_mode=True, tier="4k")
    assert model == "gpt-image-2.5-flare"
    assert reason == "auto-4k-edit"  # legacy config: edit_4k=flare


def test_model_sunburst_4k_rejected():
    with pytest.raises(PdogError) as exc:
        resolve_model(FAKE_CONFIG, model="sunburst", edit_mode=True, tier="4k")
    assert "sunburst" in exc.value.message


def test_model_invalid_name():
    with pytest.raises(PdogError):
        resolve_model(FAKE_CONFIG, model="gpt-4o", edit_mode=False, tier="1k")


# --- image input resolution ---------------------------------------------------------

def test_resolve_image_input_local_and_data_url(tmp_path):
    path = tmp_path / "ref.png"
    path.write_bytes(png_bytes((4, 4)))
    name, mime, content = resolve_image_input(str(path), "image-1")
    assert name == "ref.png" and mime == "image/png" and content.startswith(b"\x89PNG")

    raw = png_bytes((4, 4))
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    name2, mime2, content2 = resolve_image_input(data_url, "image-1")
    assert content2 == raw and mime2 == "image/png"

    with pytest.raises(PdogError):
        resolve_image_input(str(tmp_path / "missing.png"), "image-1")


# --- config loading -------------------------------------------------------------------

def test_load_config_env_overrides(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"base_url": "https://env.test/v1", "model": "m"}))
    monkeypatch.setenv("PDOG_CONFIG", str(config_file))
    monkeypatch.setenv("PDOG_API_KEY_1K", "sk-env-key")
    config = load_config()
    assert config["base_url"] == "https://env.test/v1"
    assert config["api_keys"]["1k"] == "sk-env-key"


# --- dry-run (no network) ---------------------------------------------------------------

def test_dry_run_text_to_image():
    result = run_generation(GenRequest(prompt="测试提示词", dry_run=True), FAKE_CONFIG)
    assert result["ok"] and result["dry_run"]
    preview = result["preview"]
    assert preview["tier"] == "1k"
    assert preview["model"] == "gpt-image-2.5-flare"
    assert preview["transport"] == "sync"
    assert preview["payload"]["size"] == "2048x1152"
    # real (fake) key must never leak into the result
    assert FAKE_CONFIG["api_keys"]["1k"] not in json.dumps(result)


def test_dry_run_edit_and_4k(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(png_bytes())
    result = run_generation(
        GenRequest(prompt="改", images=[str(ref)], dry_run=True), FAKE_CONFIG
    )
    preview = result["preview"]
    assert preview["mode"] == "edit"
    assert preview["model"] == "gpt-image-2.5-sunburst"
    assert preview["content_type"] == "multipart/form-data"

    result4k = run_generation(
        GenRequest(prompt="改", is_4k=True, dry_run=True), FAKE_CONFIG
    )
    preview4k = result4k["preview"]
    assert preview4k["transport"] == "async" and preview4k["tier"] == "4k"
    assert preview4k["model"] == "gpt-image-2.5-flare"
    assert preview4k.get("allow_4k_required") is True


# --- cost guards ---------------------------------------------------------------------------

def test_4k_requires_allow_4k():
    with pytest.raises(PdogError) as exc:
        run_generation(GenRequest(prompt="x", is_4k=True), FAKE_CONFIG)
    assert exc.value.kind == "cost_guard"


def test_mask_requires_image():
    with pytest.raises(PdogError) as exc:
        run_generation(GenRequest(prompt="x", mask="m.png"), FAKE_CONFIG)
    assert exc.value.kind == "invalid_parameter"


# --- full pipelines with stubbed HTTP ---------------------------------------------------------

def _stub_sync(core_module, monkeypatch, image: bytes):
    from tests.helpers import openai_b64_response

    captured = {}

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        captured["payload"] = payload
        captured["api_key"] = api_key
        captured["images"] = images
        return openai_b64_response(image)

    monkeypatch.setattr(core_module, "request_image", fake_request_image)
    return captured


def test_sync_generation_pipeline(tmp_path, monkeypatch):
    out = tmp_path / "gen.png"
    _stub_sync(core, monkeypatch, png_bytes((32, 16)))
    result = run_generation(
        GenRequest(prompt="一只猫", output=str(out)), FAKE_CONFIG
    )
    assert result["ok"] and result["transport"] == "sync"
    assert result["actual_size"] == "32x16"
    final = [f for f in result["files"] if f["role"] == "final"]
    assert len(final) == 1 and Path(final[0]["path"]).read_bytes().startswith(b"\x89PNG")
    assert Path(final[0]["path"]) == out.resolve()


def test_edit_pipeline_uses_regular_key_and_multipart(tmp_path, monkeypatch):
    ref = tmp_path / "ref.png"
    ref.write_bytes(png_bytes())
    captured = _stub_sync(core, monkeypatch, png_bytes((8, 8)))
    result = run_generation(
        GenRequest(prompt="改成夜景", images=[str(ref)], output=str(tmp_path / "e.png")),
        FAKE_CONFIG,
    )
    assert result["ok"] and result["mode"] == "edit"
    assert captured["api_key"] == FAKE_CONFIG["api_keys"]["1k"]
    assert captured["images"] and captured["images"][0][0] == "ref.png"


def test_native_transparent_success(tmp_path, monkeypatch):
    out = tmp_path / "t.png"
    _stub_sync(core, monkeypatch, png_with_transparent_margin())
    result = run_generation(
        GenRequest(prompt="透明", background="transparent", output=str(out)), FAKE_CONFIG
    )
    assert result["ok"]
    check = result["transparency_check"]
    assert check["has_alpha"] and check["verdict"] == "pass"
    assert check["meaningful_bbox"] == [5, 5, 15, 15]


def test_native_transparent_rejection_saves_upstream(tmp_path, monkeypatch):
    out = tmp_path / "t.png"
    _stub_sync(core, monkeypatch, png_bytes((8, 8)))  # fully opaque
    with pytest.raises(PdogError) as exc:
        run_generation(
            GenRequest(prompt="透明", background="transparent", output=str(out)), FAKE_CONFIG
        )
    assert exc.value.kind == "upstream"
    rejected = out.with_name("t-noalpha.png")
    assert rejected.exists()


def test_async_pipeline_done_and_pending(tmp_path, monkeypatch):
    from tests.helpers import openai_b64_response

    image = png_bytes((16, 16))
    monkeypatch.setattr(
        core, "submit_async_task",
        lambda *a, **k: "task-abc123", raising=False,
    )

    def fake_poll(base_url, api_key, task_id, *, poll_interval=5.0, task_timeout=1800.0,
                  block_seconds=None, on_event=None):
        assert task_id == "task-abc123"
        assert api_key == FAKE_CONFIG["api_keys"]["4k"]
        if block_seconds is not None:
            return {"state": "pending", "status": "processing"}
        return {"state": "done", "images": [image], "status": "completed"}

    monkeypatch.setattr(core, "poll_async_task", fake_poll, raising=False)

    pending = run_generation(
        GenRequest(prompt="4k", is_4k=True, allow_4k=True,
                   output=str(tmp_path / "p.png"), block_seconds=1.0),
        FAKE_CONFIG,
    )
    assert pending["pending"] and pending["task_id"] == "task-abc123"

    done = run_generation(
        GenRequest(prompt="4k", is_4k=True, allow_4k=True,
                   output=str(tmp_path / "d.png")),
        FAKE_CONFIG,
    )
    assert done["ok"] and done["transport"] == "async(task-abc123)"
    assert done["actual_size"] == "16x16"


def test_chroma_pipeline_end_to_end(tmp_path, monkeypatch):
    from tests.helpers import keyed_png

    ref = tmp_path / "keyed.png"
    ref.write_bytes(keyed_png())
    _stub_sync(core, monkeypatch, keyed_png())
    out = tmp_path / "cut.png"
    result = run_generation(
        GenRequest(
            prompt="抠图",
            images=[str(ref)],
            background="transparent",
            transparent_strategy="chroma-key",
            key_color="#FF00FF",
            output=str(out),
        ),
        FAKE_CONFIG,
    )
    assert result["ok"]
    assert result["chroma"]["key_color"] == "#FF00FF"
    assert result["chroma"]["transparent_percent"] > 50
    check = result["transparency_check"]
    assert check["has_alpha"] and check["verdict"] == "pass"
    saved = Image.open(out)
    assert saved.size == (64, 64)  # no resize allowed
    assert saved.getpixel((0, 0))[3] == 0  # background keyed out
    assert saved.getpixel((32, 32))[3] == 255  # subject kept


def test_chroma_dry_run_prompt_append(tmp_path):
    ref = tmp_path / "k.png"
    ref.write_bytes(keyed_png())
    result = run_generation(
        GenRequest(
            prompt="抠图",
            images=[str(ref)],
            background="transparent",
            transparent_strategy="chroma-key",
            key_color="#FF00FF",
            dry_run=True,
        ),
        FAKE_CONFIG,
    )
    assert "#FF00FF" in result["preview"]["payload"]["prompt"]


def test_chroma_skips_conversion_when_upstream_has_alpha(tmp_path, monkeypatch):
    """Upstream may ignore the key-color instruction and return real alpha."""
    upstream = png_with_transparent_margin()
    _stub_sync(core, monkeypatch, upstream)
    out = tmp_path / "cut.png"
    result = run_generation(
        GenRequest(
            prompt="抠图",
            background="transparent",
            transparent_strategy="chroma-key",
            key_color="#00FF00",
            output=str(out),
        ),
        FAKE_CONFIG,
    )
    assert result["ok"]
    assert result["chroma"]["transparent_percent"] is None
    assert result["files"][0]["bytes"] == len(upstream)
    assert result["transparency_check"]["has_alpha"]


def test_chroma_text_to_image_allows_explicit_key_color():
    """An explicit key color needs no reference image; 'auto' still does."""
    result = run_generation(
        GenRequest(
            prompt="A red apple",
            background="transparent",
            transparent_strategy="chroma-key",
            key_color="#00FF00",
            dry_run=True,
        ),
        FAKE_CONFIG,
    )
    assert "#00FF00" in result["preview"]["payload"]["prompt"]

    try:
        run_generation(
            GenRequest(
                prompt="A red apple",
                background="transparent",
                transparent_strategy="chroma-key",
                key_color="auto",
                dry_run=True,
            ),
            FAKE_CONFIG,
        )
    except PdogError as exc:
        assert "key_color" in str(exc)
    else:
        raise AssertionError("auto key color without a reference image should fail")


# --- 4K model_not_found auto-fallback to legacy image-2 ------------------------

def _stub_4k_model_not_found(monkeypatch, image):
    """Async submit+poll fails with model_not_found; sync retry succeeds."""
    from tests.helpers import openai_b64_response

    calls = {"sync": 0, "submit": 0}

    def fake_submit(*a, **k):
        calls["submit"] += 1
        return "task-dead001"

    def fake_poll(base_url, api_key, task_id, **k):
        raise PdogError(
            f"Async task {task_id} failed: "
            '{"message": "Model \\"gpt-image-2.5-flare\\" is not supported by any configured account in this group", "type": "model_not_found"}',
            kind="upstream",
        )

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        calls["sync"] += 1
        assert payload["model"] == "gpt-image-2"  # fallback model in retry
        return openai_b64_response(image)

    monkeypatch.setattr(core, "submit_async_task", fake_submit, raising=False)
    monkeypatch.setattr(core, "poll_async_task", fake_poll, raising=False)
    monkeypatch.setattr(core, "request_image", fake_request_image, raising=False)
    return calls


def test_4k_model_not_found_falls_back_to_legacy(tmp_path, monkeypatch):
    calls = _stub_4k_model_not_found(monkeypatch, png_bytes((3840 // 8, 2160 // 8)))
    result = run_generation(
        GenRequest(prompt="4k", is_4k=True, allow_4k=True,
                   output=str(tmp_path / "fb.png")),
        FAKE_CONFIG,
    )
    assert result["ok"]
    assert result["model"] == "gpt-image-2"
    assert "model_fallback" in result and "gpt-image-2.5-flare" in result["model_fallback"]
    assert calls["submit"] == 1 and calls["sync"] == 1


def test_non_4k_model_not_found_does_not_fall_back(tmp_path, monkeypatch):
    from tests.helpers import openai_b64_response

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        raise PdogError(
            "Image API returned HTTP 404: Model not supported by any configured account",
            kind="http", http_status=404,
        )

    monkeypatch.setattr(core, "request_image", fake_request_image, raising=False)
    with pytest.raises(PdogError) as exc:
        run_generation(
            GenRequest(prompt="1k", output=str(tmp_path / "x.png")), FAKE_CONFIG
        )
    assert exc.value.kind == "http"


def test_4k_sync_404_falls_back_to_legacy(tmp_path, monkeypatch):
    """Sync-route 404 also triggers exactly one legacy retry."""
    from tests.helpers import openai_b64_response

    state = {"attempts": 0}

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        state["attempts"] += 1
        if state["attempts"] == 1:
            assert payload["model"] == "gpt-image-2.5-flare"
            raise PdogError(
                "Image API returned HTTP 404: Model \"gpt-image-2.5-flare\" is not "
                "supported by any configured account in this group",
                kind="http", http_status=404,
            )
        assert payload["model"] == "gpt-image-2"
        return openai_b64_response(png_bytes((480, 270)))

    monkeypatch.setattr(core, "request_image", fake_request_image, raising=False)
    result = run_generation(
        GenRequest(prompt="4k", is_4k=True, allow_4k=True, async_mode="never",
                   output=str(tmp_path / "fb2.png")),
        FAKE_CONFIG,
    )
    assert result["ok"] and result["model"] == "gpt-image-2"
    assert state["attempts"] == 2


# --- 4K auto-routing: direct legacy model via generation_4k/edit_4k -----------

CONFIG_4K_LEGACY = dict(FAKE_CONFIG, model_auto_routing={
    "generation": "flare", "edit": "sunburst",
    "edit_4k": "image-2", "generation_4k": "image-2",
})


def test_4k_auto_routes_directly_to_legacy():
    model, reason = resolve_model(CONFIG_4K_LEGACY, edit_mode=False, tier="4k")
    assert model == "gpt-image-2" and reason == "auto-4k-generation"
    model_e, reason_e = resolve_model(CONFIG_4K_LEGACY, edit_mode=True, tier="4k")
    assert model_e == "gpt-image-2" and reason_e == "auto-4k-edit"


def test_4k_non_auto_routing_unchanged():
    # 1K/2K auto routing untouched by the 4K keys
    model, _ = resolve_model(CONFIG_4K_LEGACY, edit_mode=False, tier="1k")
    assert model == "gpt-image-2.5-flare"
    model_e, _ = resolve_model(CONFIG_4K_LEGACY, edit_mode=True, tier="1k")
    assert model_e == "gpt-image-2.5-sunburst"


def test_4k_direct_routing_makes_no_failed_attempt(tmp_path, monkeypatch):
    """With generation_4k=image-2, a 4K sync call goes straight to image-2:
    exactly one HTTP attempt, no doomed flare try, no model_fallback note."""
    from tests.helpers import openai_b64_response

    attempts = {"n": 0}

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        attempts["n"] += 1
        assert payload["model"] == "gpt-image-2"
        return openai_b64_response(png_bytes((480, 270)))

    monkeypatch.setattr(core, "request_image", fake_request_image, raising=False)
    result = run_generation(
        GenRequest(prompt="4k", is_4k=True, allow_4k=True, async_mode="never",
                   output=str(tmp_path / "direct.png")),
        CONFIG_4K_LEGACY,
    )
    assert result["ok"] and result["model"] == "gpt-image-2"
    assert result.get("model_fallback") is None
    assert attempts["n"] == 1


# --- 4K quality downgrade (gateway async validation) ---------------------------

def test_4k_quality_downgrades_to_high():
    # FAKE_CONFIG default_quality is xhigh; 4K must send high instead
    result = run_generation(
        GenRequest(prompt="4k", is_4k=True, allow_4k=True, dry_run=True),
        CONFIG_4K_LEGACY,
    )
    assert result["preview"]["payload"]["quality"] == "high"


def test_non_4k_quality_unchanged():
    result = run_generation(GenRequest(prompt="x", dry_run=True), FAKE_CONFIG)
    assert result["preview"]["payload"]["quality"] == "xhigh"
