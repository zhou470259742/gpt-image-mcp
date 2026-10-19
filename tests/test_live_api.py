"""Live API self-tests — REAL billing (≈¥0.12 total).

Run explicitly:  RUN_LIVE=1 uv run pytest tests/test_live_api.py -v -s
Covers the job-based flow: generate (job_id) -> poll -> verify -> edit -> 4K.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1",
    reason="live API tests cost money; set RUN_LIVE=1 to run",
)

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "output" / "live"
GENERATED: dict[str, str] = {}


def _payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured
    return json.loads(result.content[0].text)


async def _call(name: str, args: dict, timeout: float = 400.0) -> dict:
    params = StdioServerParameters(command=sys.executable, args=["-m", "pdog_image_mcp.server"])

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                result = await session.call_tool(name, args)
                return _payload(result)

    return await run()


async def _wait_job(session, job_id: str, deadline: float, output: str | None = None) -> dict:
    """Poll a background job until done/failed, mirroring how the model loops."""
    waited = 0.0
    while waited < deadline:
        args = {"task_id": job_id, "wait_seconds": 20.0}
        if output:
            args["output"] = output
        status = await session.call_tool("pdog_task_status", args)
        payload = _payload(status)
        waited += 20.0
        if payload.get("job_status") in ("done", "failed"):
            return payload
        print(f"[live] poll after {waited:.0f}s -> running ({payload.get('elapsed_seconds')}s)")
    raise AssertionError(f"job {job_id} still running after {deadline}s")


def test_live_1k_generation_and_verify():
    async def fn():
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out = str(OUTPUT_DIR / "cat-1k.png")
        payload = await _call("pdog_generate", {
            "prompt": "一只戴宇航员头盔的橘猫，扁平插画风格，简洁背景",
            "size": "1024x1024",
            "output": out,
        }, timeout=500)
        assert payload["ok"], payload
        assert payload["pending"] and payload["job_id"]
        print(f"\n[live] job submitted: {payload['job_id']}")

        params = StdioServerParameters(command=sys.executable, args=["-m", "pdog_image_mcp.server"])
        async def run():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=400)) as session:
                    await session.initialize()
                    return await _wait_job(session, payload["job_id"], deadline=240.0)
        payload = await run()

        assert payload["ok"], payload
        assert payload["job_status"] == "done"
        final = [f for f in payload["files"] if f["role"] == "final"]
        assert final and Path(final[0]["path"]).stat().st_size > 10_000
        assert final[0]["width"] and final[0]["height"]
        GENERATED["image"] = final[0]["path"]
        print(f"[live] 1K generated: {final[0]['path']} "
              f"actual={payload.get('actual_size')} model={payload['model']}")

        verify = await _call("pdog_verify_transparency", {"image_path": GENERATED["image"]})
        assert verify["ok"]
        print(f"[live] transparency verdict: {verify['verdict']} (expected opaque)")
        assert verify["verdict"] == "opaque"

    asyncio.run(fn())


def test_live_edit_multipart():
    async def fn():
        # reuse the 1K image from the generation test (or a previous run) to
        # avoid paying for a fresh generation when tests run selectively
        source = GENERATED.get("image")
        if not source:
            fallback = OUTPUT_DIR / "cat-1k.png"
            assert fallback.exists(), "run test_live_1k_generation_and_verify first"
            source = str(fallback)
        out = str(OUTPUT_DIR / "cat-edit.png")
        payload = await _call("pdog_edit", {
            "prompt": "把背景改为纯深蓝色 #102A43，其他内容保持不变，不添加新元素",
            "images": [source],
            "output": out,
        }, timeout=500)
        assert payload["ok"], payload
        assert payload["pending"] and payload["job_id"]

        params = StdioServerParameters(command=sys.executable, args=["-m", "pdog_image_mcp.server"])
        async def run():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=400)) as session:
                    await session.initialize()
                    return await _wait_job(session, payload["job_id"], deadline=240.0)
        payload = await run()

        assert payload["ok"] and payload["job_status"] == "done", payload
        assert payload["mode"] == "edit"
        assert payload["model"] == "gpt-image-2.5-sunburst"
        final = [f for f in payload["files"] if f["role"] == "final"]
        assert final and Path(final[0]["path"]).stat().st_size > 10_000
        GENERATED["edit"] = final[0]["path"]
        print(f"\n[live] edit saved: {final[0]['path']} actual={payload.get('actual_size')}")

    asyncio.run(fn())


def test_live_4k_fallback_flow():
    async def fn():
        out = str(OUTPUT_DIR / "cat-4k.png")
        payload = await _call("pdog_generate", {
            "prompt": "超高清星空下的雪山湖泊，细节丰富，4K 壁纸",
            "size": "3840x2160",
            "allow_4k": True,
            "output": out,
        }, timeout=500)
        assert payload["ok"], payload
        assert payload["pending"] and payload["job_id"]
        print(f"\n[live] 4K job submitted: {payload['job_id']}")

        params = StdioServerParameters(command=sys.executable, args=["-m", "pdog_image_mcp.server"])
        async def run():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=500)) as session:
                    await session.initialize()
                    return await _wait_job(session, payload["job_id"], deadline=480.0)
        payload = await run()

        assert payload["ok"] and payload["job_status"] == "done", payload
        assert payload["tier"] == "4k"
        final = [f for f in payload["files"] if f["role"] == "final"]
        assert final and Path(final[0]["path"]).stat().st_size > 100_000
        print(f"[live] 4K saved: {final[0]['path']} "
              f"actual={final[0]['width']}x{final[0]['height']} "
              f"model={payload.get('model')} fallback={payload.get('model_fallback')}")

    asyncio.run(fn())
