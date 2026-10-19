"""MCP stdio protocol integration tests (free: dry-run, guards, no API calls).

Each test spawns the real server over stdio, exactly like a ZCode session does.
The server is pointed at an unreachable local config with fake keys, so any
accidental network attempt fails instantly at connect time — these tests can
never produce real API traffic or billing.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from PIL import Image

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_ARGS = ["-m", "pdog_image_mcp.server"]


@pytest.fixture()
def isolated_env(tmp_path_factory):
    """Config pointing at an unreachable local endpoint + fake keys."""
    config_path = tmp_path_factory.mktemp("cfg") / "config.json"
    config_path.write_text(json.dumps({
        "base_url": "https://127.0.0.1:9",
        "model": "gpt-image-2.5-flare",
        "models": {
            "flare": "gpt-image-2.5-flare",
            "sunburst": "gpt-image-2.5-sunburst",
            "image-2": "gpt-image-2",
        },
        "model_default_alias": "flare",
        "model_auto_routing": {"generation": "flare", "edit": "sunburst", "edit_4k": "flare"},
        "async": {"mode": "auto", "poll_interval_seconds": 1, "task_timeout_seconds": 10},
        "default_resolution": "1k",
        "default_quality": "xhigh",
        "timeout_seconds": 5,
    }))
    return {
        **os.environ,
        "PDOG_CONFIG": str(config_path),
        "PDOG_API_KEY_1K": "sk-test-regular-key-000000000000",
        "PDOG_API_KEY_4K": "sk-test-4k-key-11111111111111",
    }


def _run(coro):
    return asyncio.run(coro)


async def _with_session(fn, env=None):
    params = StdioServerParameters(command=sys.executable, args=SERVER_ARGS, env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured
    return json.loads(result.content[0].text)


def _png(tmp_path, name="t.png", size=(20, 20), box=(5, 5, 15, 15)):
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((200, 30, 30, 255), box)
    path = tmp_path / name
    path.write_bytes(b"")
    buffer = __import__("io").BytesIO()
    image.save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())
    return str(path)


async def _poll_job(session, job_id: str, deadline_seconds: float = 60.0) -> dict:
    """Poll a background job to completion (free tests only: jobs fail fast)."""
    waited = 0.0
    while waited < deadline_seconds:
        status = await session.call_tool(
            "pdog_task_status", {"task_id": job_id, "wait_seconds": 5}
        )
        payload = _payload(status)
        if payload.get("job_status") in ("done", "failed"):
            return payload
        waited += 5.0
    raise AssertionError(f"job {job_id} still running after {deadline_seconds}s")


def test_handshake_tools_and_resources():
    async def fn(session):
        tools = await session.list_tools()
        names = {t.name for t in tools.tools}
        assert {
            "pdog_generate", "pdog_edit", "pdog_task_status",
            "pdog_verify_transparency", "pdog_chroma_key",
        } <= names

        resources = await session.list_resources()
        uris = {str(r.uri) for r in resources.resources}
        assert "pdog://docs/api-spec" in uris

        doc = await session.read_resource("pdog://docs/api-spec")
        text = doc.contents[0].text
        assert "images/generations" in text

    _run(_with_session(fn))


def test_generate_dry_run_and_guards(tmp_path, isolated_env):
    async def fn(session):
        # dry run: routing preview without any API call
        result = await session.call_tool(
            "pdog_generate", {"prompt": "测试", "dry_run": True}
        )
        payload = _payload(result)
        assert payload["ok"] and payload["dry_run"]
        preview = payload["preview"]
        assert preview["tier"] == "1k" and preview["model"].endswith("flare")
        masked = preview["api_key"]
        assert "..." in masked and len(masked) < 20

        # 4K billed route requires explicit confirmation (fires before any network)
        result = await session.call_tool(
            "pdog_generate", {"prompt": "测试", "size": "3840x2160"}
        )
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "cost_guard"

        # invalid size never reaches the API
        result = await session.call_tool(
            "pdog_generate", {"prompt": "测试", "size": "123x456"}
        )
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "invalid_parameter"

        # edit without images is rejected before any network activity
        result = await session.call_tool(
            "pdog_edit", {"prompt": "测试", "images": []}
        )
        payload = _payload(result)
        assert payload["ok"] is False

        # a real (non-dry) 1K call returns a job_id immediately (background
        # render); polling the job surfaces the connect failure cleanly
        result = await session.call_tool(
            "pdog_generate", {"prompt": "测试", "size": "1024x1024"}
        )
        payload = _payload(result)
        assert payload["ok"] and payload["pending"] and payload["job_id"]

        status = await _poll_job(session, payload["job_id"])
        assert status["ok"] is False
        assert status["job_status"] == "failed"
        assert status["error"]["kind"] in ("network", "http", "upstream")

        # unknown job ids are reported cleanly
        result = await session.call_tool(
            "pdog_task_status", {"task_id": "job_doesnotexist"}
        )
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "invalid_parameter"

    _run(_with_session(fn, env=isolated_env))


def test_verify_transparency_and_chroma_key(tmp_path, isolated_env):
    async def fn(session):
        path = _png(tmp_path)
        result = await session.call_tool("pdog_verify_transparency", {"image_path": path})
        payload = _payload(result)
        assert payload["ok"] and payload["verdict"] == "pass"
        assert payload["meaningful_bbox"] == [5, 5, 15, 15]

        result = await session.call_tool(
            "pdog_verify_transparency", {"image_path": str(tmp_path / "missing.png")}
        )
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "invalid_parameter"

        # chroma-key on a solid magenta background image (free, no API);
        # explicit key color because 'auto' would pick a non-colliding color
        keyed = tmp_path / "keyed.png"
        image = Image.new("RGBA", (64, 64), (255, 0, 255, 255))
        image.paste((0, 200, 0, 255), (22, 22, 42, 42))
        buffer = __import__("io").BytesIO()
        image.save(buffer, format="PNG")
        keyed.write_bytes(buffer.getvalue())

        result = await session.call_tool(
            "pdog_chroma_key", {"image_path": str(keyed), "key_color": "#FF00FF"}
        )
        payload = _payload(result)
        assert payload["ok"], payload
        assert payload["metrics"]["key_color"] == "#FF00FF"
        assert payload["check"]["has_alpha"]

        # 'auto' on this image picks a low-collision color (not magenta), and
        # extraction then fails cleanly with a structured error, not a crash
        result = await session.call_tool("pdog_chroma_key", {"image_path": str(keyed)})
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"]["kind"] in ("upstream", "invalid_parameter")

    _run(_with_session(fn, env=isolated_env))


def test_cli_dry_run_stdout_is_clean_json():
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "pdog_image_mcp.cli", "--dry-run", "--prompt", "测试"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] and payload["preview"]["tier"] == "1k"
