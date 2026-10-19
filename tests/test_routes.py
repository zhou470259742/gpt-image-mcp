"""Tests for multi-route configs: per-tier endpoints, mode-specific models, failover."""

from __future__ import annotations

import pytest

from pdog_image_mcp import core
from pdog_image_mcp.core import (
    GenRequest,
    PdogError,
    normalize_routes,
    resolve_model,
    resolve_tier,
    routes_for_tier,
    run_generation,
)
from tests.helpers import openai_b64_response, png_bytes

MULTI_ROUTE_CONFIG = {
    # Global defaults kept for shared settings only.
    "model": "gpt-image-2.5-flare",
    "default_resolution": "1k",
    "default_quality": "xhigh",
    "timeout_seconds": 300,
    "async": {"mode": "auto", "poll_interval_seconds": 5, "task_timeout_seconds": 1800},
    "routes": [
        {
            "name": "main",
            "base_url": "https://main.test/v1",
            "api_keys": {"1k": "sk-main-1k-aaaaaaaaaaaaaaaaaaaa", "4k": "sk-main-4k-bbbbbbbbbbbbbbbbbbbb"},
            "generation": "gen-model-main",
            "edit": "edit-model-main",
            "generation_4k": "gen4k-model-main",
            "edit_4k": "edit4k-model-main",
        },
        {
            "name": "backup",
            "base_url": "https://backup.test/v1",
            "api_keys": {"1k": "sk-backup-1k-cccccccccccccccccc", "4k": "sk-backup-4k-dddddddddddddddddddd"},
            "generation": "gen-model-backup",
            "edit": "edit-model-backup",
        },
    ],
}


def test_legacy_config_normalizes_to_single_route():
    legacy = {
        "base_url": "https://legacy.test",
        "model": "m",
        "api_keys": {"1k": "k1", "4k": "k4"},
    }
    routes = normalize_routes(legacy)
    assert len(routes) == 1
    assert routes[0].base_url == "https://legacy.test"
    assert routes[0].key_for("1k") == "k1" and routes[0].key_for("4k") == "k4"


def test_routes_preserve_configured_order():
    routes = normalize_routes(MULTI_ROUTE_CONFIG)
    assert [route.name for route in routes] == ["main", "backup"]
    assert routes[0].base_url == "https://main.test/v1"


def test_per_tier_endpoint_and_key_selection():
    tier, key, size = resolve_tier(MULTI_ROUTE_CONFIG, size="2048x1152")
    assert tier == "2k" and key == "sk-main-1k-aaaaaaaaaaaaaaaaaaaa"
    assert size == "2048x1152"

    tier4k, key4k, _ = resolve_tier(MULTI_ROUTE_CONFIG, is_4k=True)
    assert tier4k == "4k" and key4k == "sk-main-4k-bbbbbbbbbbbbbbbbbbbb"

    # A run on each tier must hit that tier's own endpoint, not a shared one.
    preview_2k = run_generation(
        GenRequest(prompt="x", size="2048x1152", dry_run=True), MULTI_ROUTE_CONFIG
    )["preview"]
    assert preview_2k["endpoint"].startswith("https://main.test/v1")
    assert preview_2k["tier"] == "2k"

    preview_4k = run_generation(
        GenRequest(prompt="x", is_4k=True, allow_4k=True, dry_run=True), MULTI_ROUTE_CONFIG
    )["preview"]
    assert preview_4k["endpoint"].startswith("https://main.test/v1")
    assert preview_4k["tier"] == "4k"


def test_routes_can_be_tier_scoped():
    config = {
        "routes": [
            {
                "name": "regular",
                "base_url": "https://a.test",
                "api_key": "k1",
                "generation": "g-1k",
                "tiers": ["1k", "2k"],
            },
            {
                "name": "big",
                "base_url": "https://b.test",
                "api_key": "k4",
                "generation": "g-4k",
                "tiers": ["4k"],
            },
        ]
    }
    assert [r.name for r in routes_for_tier(config, "2k")] == ["regular"]
    assert [r.name for r in routes_for_tier(config, "4k")] == ["big"]
    assert resolve_tier(config, is_4k=True)[1] == "k4"


def test_generation_and_edit_models_are_separate():
    gen, _ = resolve_model(MULTI_ROUTE_CONFIG, edit_mode=False, tier="1k")
    edit, _ = resolve_model(MULTI_ROUTE_CONFIG, edit_mode=True, tier="1k")
    assert gen == "gen-model-main"
    assert edit == "edit-model-main"

    gen4k, _ = resolve_model(MULTI_ROUTE_CONFIG, edit_mode=False, tier="4k")
    edit4k, _ = resolve_model(MULTI_ROUTE_CONFIG, edit_mode=True, tier="4k")
    assert gen4k == "gen4k-model-main"
    assert edit4k == "edit4k-model-main"


def test_edit_model_falls_back_to_generation_when_unset():
    config = {
        "routes": [
            {"name": "only-gen", "base_url": "https://a.test", "api_key": "k", "generation": "g-model"}
        ]
    }
    edit, _ = resolve_model(config, edit_mode=True, tier="1k")
    assert edit == "g-model"


def _stub_routes(monkeypatch, *, first_fails: bool, image=None):
    """Record every endpoint attempt; optionally fail the first route."""
    image = image or png_bytes((16, 16))
    attempts = []

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        attempts.append({"base_url": base_url, "api_key": api_key, "model": payload["model"]})
        if first_fails and len(attempts) == 1:
            raise PdogError("Image API returned HTTP 503: upstream down",
                            kind="http", http_status=503)
        return openai_b64_response(image)

    monkeypatch.setattr(core, "request_image", fake_request_image)
    return attempts


def test_failover_to_backup_route(tmp_path, monkeypatch):
    attempts = _stub_routes(monkeypatch, first_fails=True)
    result = run_generation(
        GenRequest(prompt="cat", output=str(tmp_path / "o.png")), MULTI_ROUTE_CONFIG
    )
    assert result["ok"]
    assert [a["base_url"] for a in attempts] == ["https://main.test/v1", "https://backup.test/v1"]
    # The backup route used its own key and its own model.
    assert attempts[1]["api_key"] == "sk-backup-1k-cccccccccccccccccc"
    assert attempts[1]["model"] == "gen-model-backup"
    assert result["route"] == "backup"
    assert "route_fallback" in result and "main" in result["route_fallback"]
    assert result["model"] == "gen-model-backup"


def test_primary_route_used_when_healthy(tmp_path, monkeypatch):
    attempts = _stub_routes(monkeypatch, first_fails=False)
    result = run_generation(
        GenRequest(prompt="cat", output=str(tmp_path / "o.png")), MULTI_ROUTE_CONFIG
    )
    assert len(attempts) == 1
    assert result["route"] == "main"
    assert result["route_fallback"] is None
    assert result["model"] == "gen-model-main"


def test_auth_error_still_fails_over():
    """401 means the key cannot serve this tier; another route may."""
    config = MULTI_ROUTE_CONFIG
    routes = routes_for_tier(config, "1k")
    assert len(routes) == 2


def test_timeout_does_not_fail_over(tmp_path, monkeypatch):
    """A sent request may already be billed, so a timeout must not resubmit."""
    attempts = []

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        attempts.append(base_url)
        raise PdogError("Could not reach Image API: timed out", kind="network", failover_safe=False)

    monkeypatch.setattr(core, "request_image", fake_request_image)
    with pytest.raises(PdogError):
        run_generation(
            GenRequest(prompt="cat", output=str(tmp_path / "o.png")), MULTI_ROUTE_CONFIG
        )
    assert attempts == ["https://main.test/v1"]  # backup never attempted


def test_all_routes_failing_reports_last_error(tmp_path, monkeypatch):
    attempts = []

    def fake_request_image(base_url, api_key, payload, images=None, mask=None,
                           timeout_seconds=300.0, max_retries=2):
        attempts.append(base_url)
        raise PdogError(f"HTTP 500 from {base_url}", kind="http", http_status=500)

    monkeypatch.setattr(core, "request_image", fake_request_image)
    with pytest.raises(PdogError) as exc:
        run_generation(GenRequest(prompt="cat", output=str(tmp_path / "o.png")), MULTI_ROUTE_CONFIG)
    assert len(attempts) == 2
    assert "backup.test" in exc.value.message
    assert exc.value.route == "backup"


def test_route_without_key_for_tier_is_skipped():
    config = {
        "routes": [
            {"name": "no-4k", "base_url": "https://a.test", "api_key_1k": "k1"},
            {"name": "big", "base_url": "https://b.test", "api_key_4k": "k4"},
        ]
    }
    schedule_4k = [r.name for r in routes_for_tier(config, "4k") if r.key_for("4k")]
    assert schedule_4k == ["big"]
    assert resolve_tier(config, is_4k=True)[1] == "k4"


def test_missing_key_for_tier_raises_config_error():
    config = {"routes": [{"name": "no-4k", "base_url": "https://a.test", "api_key_1k": "k1"}]}
    with pytest.raises(PdogError) as exc:
        run_generation(GenRequest(prompt="x", is_4k=True, allow_4k=True), config)
    assert exc.value.kind == "config"


def test_explicit_model_not_on_route_raises(tmp_path, monkeypatch):
    config = {
        "routes": [
            {
                "name": "r",
                "base_url": "https://a.test",
                "api_key": "k",
                "generation": "gen-a",
                "edit": "edit-a",
            }
        ]
    }
    _stub_routes(monkeypatch, first_fails=False)
    with pytest.raises(PdogError) as exc:
        run_generation(
            GenRequest(prompt="x", model="sunburst", output=str(tmp_path / "o.png")), config
        )
    assert exc.value.kind == "invalid_parameter"


def test_dry_run_lists_fallback_routes():
    preview = run_generation(GenRequest(prompt="x", dry_run=True), MULTI_ROUTE_CONFIG)["preview"]
    assert preview["route"] == "main"
    assert [r["route"] for r in preview["fallback_routes"]] == ["backup"]
