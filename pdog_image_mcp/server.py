"""FastMCP server exposing the Pdog image engine as MCP tools.

Run with: uv run --project <plugin root> pdog-image-mcp   (stdio transport)

Timeout-aware design: MCP clients typically abort tool calls at 30s, while
image renders take 20-120s. Billed calls therefore return a job_id within
seconds and render in a background thread; pdog_task_status polls the job
store. Each tool call stays far below any client timeout, and a client-side
timeout can no longer orphan a paid task.

Tools:
- pdog_generate / pdog_edit : billed generation & editing (job-based)
- pdog_task_status          : poll a job_id (or a legacy upstream task_id)
- pdog_verify_transparency  : free static alpha/BBox verification
- pdog_chroma_key           : free standalone chroma-key extraction

The 4K cost guard is protocol-level: billed 4K calls require allow_4k=true.
API keys never appear in tool results or logs (masked).
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP

from . import transparency
from .core import (
    GenRequest,
    PdogError,
    chroma_key_file,
    collect_async_result,
    config_secret_values,
    load_config,
    mask_key,
    resolve_image_input,
    resolve_model,
    resolve_tier,
    run_generation,
)

mcp = FastMCP(
    "pdog-image",
    instructions=(
        "Pdog GPT Image 2.5 生图/编辑工具（技能 pdog-gpt-image2 的 MCP 封装）。\n"
        "- 编辑默认只传 images + prompt：把保留项、删除项、修改项完整写进 prompt；"
        "仅当用户显性要求『蒙版/局部重绘/只改这个区域』时才传 mask。\n"
        "- 4K 仅在用户显性要求时调用，且必须 allow_4k=true（协议级扣费守卫；"
        "1K/2K 约 ¥0.02/张，4K 约 ¥0.08/张，不要为『质量可能更好』升级 4K）。\n"
        "- 计费调用立即返回 job_id，图像在后台生成（约 20-120s）：用 "
        "pdog_task_status(job_id) 轮询直到完成，切勿重复提交同一请求。\n"
        "- 透明背景默认要求原生 Alpha（background='transparent'）；无透明像素即判失败，"
        "色键仅作为用户显式接受的回退（transparent_strategy='chroma-key'）。\n"
        "- 交付时必须如实转述上游实际返回的像素尺寸（可能小于请求尺寸），绝不虚标。\n"
        "- 网关实况：4K 账户组不提供 2.5 模型族，4K 默认直接使用 gpt-image-2（生成与编辑同）；\n"
        "  显式指定的模型若不可用会自动回落并在 model_fallback 字段标注，交付时如实转述实际模型。\n"
        "- 参考文档 resources: pdog://docs/api-spec, pdog://docs/sizes, "
        "pdog://docs/errors, pdog://docs/chroma-key。"
    ),
)

# Background job store: job_id -> state dict. The MCP server lives for the
# whole session, so in-memory jobs cover every tool call in that session.
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOB_WAIT_CAP = 25.0  # stays below the typical 30s client tool timeout

# Legacy upstream async tasks: task_id -> the GenRequest that submitted it.
TASKS: dict[str, GenRequest] = {}

DEFAULT_OUTPUT_DIR = Path.home() / "pdog-image-output"
DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"


def _default_output(kind: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return str(DEFAULT_OUTPUT_DIR / f"pdog-{kind}-{timestamp}.png")


def _sanitize(text: str, config: dict[str, Any]) -> str:
    for secret in config_secret_values(config):
        if secret:
            text = text.replace(secret, mask_key(secret))
    return text


def _error_result(exc: Exception, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if isinstance(exc, PdogError):
        return {"ok": False, "error": exc.to_dict()}
    message = f"{type(exc).__name__}: {exc}"
    if config is not None:
        message = _sanitize(message, config)
    # Unexpected errors carry a short traceback tail so failures stay diagnosable
    # from the tool result alone.
    import traceback

    tail = traceback.format_exception(exc)[-1].strip()
    return {
        "ok": False,
        "error": {"kind": "unexpected", "message": message, "at": tail},
    }


def _start_job(req: GenRequest, config: dict[str, Any]) -> str:
    """Kick off a billed render in a background thread; returns the job id."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    job: dict[str, Any] = {
        "job_id": job_id,
        "state": "running",
        "created": time.time(),
        "events": [],
        "result": None,
        "error": None,
        "model": None,
    }
    with JOBS_LOCK:
        JOBS[job_id] = job

    def on_event(event: dict) -> None:
        with JOBS_LOCK:
            job["events"].append(event)
            if event.get("type") == "submitted":
                job["model"] = event.get("model")

    def run() -> None:
        try:
            result = run_generation(req, config, on_event=on_event)
            with JOBS_LOCK:
                job["result"] = result
                job["state"] = "done"
        except Exception as exc:  # noqa: BLE001 - job boundary
            with JOBS_LOCK:
                job["error"] = _error_result(exc, config)
                job["state"] = "failed"

    threading.Thread(target=run, name=f"pdog-job-{job_id}", daemon=True).start()
    return job_id


def _job_snapshot(job_id: str) -> Optional[dict[str, Any]]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job_id,
            "state": job["state"],
            "created": job["created"],
            "events": list(job["events"]),
            "result": job["result"],
            "error": job["error"],
            "model": job["model"],
        }


async def _await_job(job_id: str, wait_seconds: float) -> dict[str, Any]:
    """Return the job state, waiting up to wait_seconds (capped at 25s)."""
    deadline = time.monotonic() + min(max(wait_seconds, 0.0), JOB_WAIT_CAP)
    while True:
        job = _job_snapshot(job_id)
        if job is None:
            return {
                "ok": False,
                "error": {
                    "kind": "invalid_parameter",
                    "message": f"Unknown job_id: {job_id}",
                    "hint": "job_id 由 pdog_generate/pdog_edit 返回；仅本会话内的任务可查询。",
                },
            }
        elapsed = round(time.time() - job["created"], 1)
        if job["state"] == "done":
            return {**job["result"], "job_id": job_id, "job_status": "done", "elapsed_seconds": elapsed}
        if job["state"] == "failed":
            return {
                "ok": False,
                "job_id": job_id,
                "job_status": "failed",
                "elapsed_seconds": elapsed,
                **(job["error"] or {}),
            }
        if time.monotonic() >= deadline:
            last_event = job["events"][-1] if job["events"] else None
            return {
                "ok": True,
                "pending": True,
                "job_id": job_id,
                "job_status": "running",
                "model": job["model"],
                "elapsed_seconds": elapsed,
                "last_event": last_event,
                "note": "仍在生成（已计费，切勿重复提交）；建议 wait_seconds=20 轮询以减少调用次数，直至完成。",
            }
        await asyncio.sleep(1.0)


def _build_request(
    *,
    prompt: str,
    images: Optional[list[str]],
    mask: Optional[str],
    size: Optional[str],
    quality: Optional[str],
    n: int,
    model: str,
    background: Optional[str],
    transparent_strategy: str,
    key_color: str,
    allow_4k: bool,
    dry_run: bool,
    async_mode: str,
    output: Optional[str],
    is_4k: bool = False,
) -> GenRequest:
    return GenRequest(
        prompt=prompt,
        images=list(images or []),
        mask=mask,
        size=size,
        is_4k=is_4k,
        quality=quality,
        n=n,
        model=model,
        background=background,
        transparent_strategy=transparent_strategy,
        key_color=key_color,
        allow_4k=allow_4k,
        dry_run=dry_run,
        async_mode=async_mode,
        output=output,
        block_seconds=None,  # background jobs run to completion
    )


async def _generate_impl(req: GenRequest, wait_seconds: float) -> dict[str, Any]:
    config: Optional[dict[str, Any]] = None
    try:
        config = load_config()
        if req.dry_run:
            return await asyncio.to_thread(run_generation, req, config)
        # Pre-flight: parameter and cost-guard errors fail synchronously, before
        # a job is spawned, so the model sees them immediately.
        tier, _key, _size = resolve_tier(
            config, is_4k=req.is_4k, resolution=req.resolution, size=req.size
        )
        if tier == "4k" and not req.allow_4k:
            raise PdogError(
                "4K 生成走付费 4K 路由（约 ¥0.08/张），必须显式确认。",
                kind="cost_guard",
                hint="用户明确要求 4K 后，携带 allow_4k=true 重新调用本工具。",
            )
        resolve_model(config, model=req.model, edit_mode=bool(req.images), tier=tier)
        job_id = _start_job(req, config)
        return await _await_job(job_id, wait_seconds)
    except Exception as exc:
        return _error_result(exc, config)


@mcp.tool()
async def pdog_generate(
    prompt: str,
    size: Optional[str] = None,
    quality: Optional[str] = None,
    n: int = 1,
    model: str = "auto",
    background: Optional[str] = None,
    transparent_strategy: str = "auto",
    key_color: str = "auto",
    allow_4k: bool = False,
    dry_run: bool = False,
    async_mode: str = "auto",
    output: Optional[str] = None,
    wait_seconds: float = 0.0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Pdog 文生图（模型由配置的路由决定）。立即返回 job_id，图像在后台生成
    （约 20-120s），用 pdog_task_status(job_id) 轮询直到完成，切勿重复提交。

    档位：默认 1K（2048x1152，约 ¥0.02/张）；4K（如 3840x2160，约 ¥0.08/张）仅在用户
    显性要求时调用且必须 allow_4k=true。常用尺寸：16:9 2048x1152、3:2 1536x1024、
    1:1 1024x1024、2:3 1024x1536。
    透明背景：background='transparent'（默认要求原生 Alpha，上游无透明像素会判失败；
    色键回退需用户显式接受后用 transparent_strategy='chroma-key'）。
    多路由：主路由在未计费的情况下失败时会自动切到备用路由，结果里的 route 字段是
    实际使用的路由，route_fallback 记录切换原因；交付时应如实转述这两项。
    dry_run=true 时同步返回路由与请求预览，不扣费。交付时如实转述上游实际像素尺寸。
    """
    req = _build_request(
        prompt=prompt,
        images=None,
        mask=None,
        size=size,
        quality=quality,
        n=n,
        model=model,
        background=background,
        transparent_strategy=transparent_strategy,
        key_color=key_color,
        allow_4k=allow_4k,
        dry_run=dry_run,
        async_mode=async_mode,
        output=output or _default_output("image"),
    )
    return await _generate_impl(req, wait_seconds)


@mcp.tool()
async def pdog_edit(
    prompt: str,
    images: list[str],
    mask: Optional[str] = None,
    size: Optional[str] = None,
    quality: Optional[str] = None,
    n: int = 1,
    model: str = "auto",
    background: Optional[str] = None,
    transparent_strategy: str = "auto",
    key_color: str = "auto",
    allow_4k: bool = False,
    dry_run: bool = False,
    async_mode: str = "auto",
    output: Optional[str] = None,
    wait_seconds: float = 0.0,
    ctx: Context = None,
) -> dict[str, Any]:
    """Pdog 图片编辑（默认模型 sunburst，按提示词整图编辑）。立即返回 job_id，
    用 pdog_task_status(job_id) 轮询，切勿重复提交。

    默认只传 images + prompt：将保留项、删除项、修改项和禁止新增项完整写进 prompt；
    不要自行制作或推断蒙版。仅当用户显性说『使用蒙版』『局部重绘』『只修改这个区域』
    或直接提供蒙版时才传 mask（透明区域=编辑区）。
    用户已给出简洁可执行的提示词时逐字使用，不要扩写成保留/删除清单。
    images 接受本地路径/URL/data URL，可多张。4K 需用户显性要求且 allow_4k=true。
    dry_run=true 只预览不扣费。交付时如实转述上游实际像素尺寸。
    """
    if not images:
        return {
            "ok": False,
            "error": {
                "kind": "invalid_parameter",
                "message": "pdog_edit requires at least one reference image.",
                "hint": "纯文生图请改用 pdog_generate。",
            },
        }
    req = _build_request(
        prompt=prompt,
        images=images,
        mask=mask,
        size=size,
        quality=quality,
        n=n,
        model=model,
        background=background,
        transparent_strategy=transparent_strategy,
        key_color=key_color,
        allow_4k=allow_4k,
        dry_run=dry_run,
        async_mode=async_mode,
        output=output or _default_output("edit"),
    )
    return await _generate_impl(req, wait_seconds)


@mcp.tool()
async def pdog_task_status(
    task_id: str,
    wait_seconds: float = 0.0,
    output: Optional[str] = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """查询生成任务状态：job_id（pdog_generate/pdog_edit 返回）或上游异步 task_id。

    wait_seconds（≤25）内可阻塞等待完成。running=继续轮询（任务已计费，切勿重复提交）；
    完成时返回文件路径与实际像素尺寸；failed 时返回原因与建议。
    """
    config: Optional[dict[str, Any]] = None
    try:
        config = load_config()
        job = _job_snapshot(task_id)
        if job is not None:
            return await _await_job(task_id, wait_seconds)
        if task_id.startswith("job_"):
            return {
                "ok": False,
                "error": {
                    "kind": "invalid_parameter",
                    "message": f"Unknown job_id: {task_id}",
                    "hint": "job_id 由 pdog_generate/pdog_edit 返回；仅本会话内的任务可查询。",
                },
            }

        # Legacy: a real upstream async task id (e.g. imgtask_...) — poll upstream.
        req = TASKS.get(task_id)
        if req is not None and output:
            req.output = output
        elif req is None:
            req = GenRequest(prompt="(async task continuation)", output=output)
        bounded_wait = min(max(wait_seconds, 0.0), JOB_WAIT_CAP)
        result = await asyncio.to_thread(
            collect_async_result,
            config,
            task_id,
            req,
            wait_seconds=bounded_wait,
            on_event=None,
        )
        if result.get("pending"):
            TASKS[task_id] = req
        return result
    except Exception as exc:
        return _error_result(exc, config)


@mcp.tool()
def pdog_verify_transparency(image_path: str) -> dict[str, Any]:
    """验证 PNG 是否有真实透明像素，并按技能规则给出验收判定（免费，不调用 API）。

    返回 alpha 统计、alpha>=8 的有意义 BBox 与四周留白、边缘接触分析：
    alpha>=16 实质轮廓碰边= fail；仅 1~15 低 Alpha 软边= review（softEdgeContact，
    需人工棋盘格/白底/黑底复核）；否则 pass。image_path 支持本地路径/URL/data URL。
    """
    try:
        _filename, _mime, content = resolve_image_input(image_path, "image")
        report = transparency.verify_transparency(content)
        report["image"] = image_path
        return {"ok": True, **report}
    except PdogError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except Exception as exc:
        return {"ok": False, "error": {"kind": "unexpected", "message": str(exc)}}


@mcp.tool()
def pdog_chroma_key(
    image_path: str,
    key_color: str = "auto",
    tolerance: float = 30.0,
    feather: float = 40.0,
    despill_distance: float = 55.0,
    despill_radius: int = 6,
    output: Optional[str] = None,
) -> dict[str, Any]:
    """对已有图像独立执行动态色键转 Alpha（免费，不调用 API，不缩放）。

    用于：色键回退结果的重新抠图（调整 tolerance/feather 无需重新生成扣费）、
    或对纯色背景素材去背。key_color='auto' 自动选冲突最小的高饱和纯色。
    仅将与画布边缘连通的色键区域转为 Alpha 并清理色边，交付前需目检。
    需要项目安装 --extra chroma（opencv/numpy）。
    """
    try:
        filename, _mime, content = resolve_image_input(image_path, "image")
        out_bytes, metrics = chroma_key_file(
            content,
            key_color=key_color,
            tolerance=tolerance,
            feather=feather,
            despill_distance=despill_distance,
            despill_radius=despill_radius,
        )
        out_path = Path(output).expanduser() if output else (
            DEFAULT_OUTPUT_DIR / f"{Path(filename).stem}-alpha-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        )
        out_path = out_path.resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(out_bytes)
        report = transparency.verify_transparency(out_bytes)
        return {"ok": True, "output": str(out_path), "metrics": metrics, "check": report}
    except PdogError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except Exception as exc:
        return {"ok": False, "error": {"kind": "unexpected", "message": str(exc)}}


# ---------------------------------------------------------------------------
# Resources: bundled reference documents
# ---------------------------------------------------------------------------

_DOCS = {
    "pdog://docs/api-spec": ("api-spec.md", "Pdog Images API 完整参数规范与调用 Schema"),
    "pdog://docs/sizes": ("size-resolution-fallback.md", "尺寸支持矩阵与 1K/2K/4K 分流规则"),
    "pdog://docs/errors": ("error-codes.md", "错误码对照与排查指南"),
    "pdog://docs/chroma-key": ("chroma-key-transparency.md", "动态色键透明的选色、抠除与验收规则"),
}


def _read_doc(filename: str) -> str:
    path = DOCS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return f"(document {filename} is not bundled in this installation)"


for _uri, (_filename, _description) in _DOCS.items():
    def _make_reader(fname: str):
        def _reader() -> str:
            return _read_doc(fname)
        _reader.__name__ = f"read_{fname.replace('-', '_').replace('.', '_')}"
        return _reader

    mcp.resource(_uri, name=_filename, description=_description)(_make_reader(_filename))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
