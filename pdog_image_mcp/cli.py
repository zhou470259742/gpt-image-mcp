"""Skill-parity CLI over the same engine (pdog-image).

Mirrors the original pdog-gpt-image2 generate_image.py flags; prints the
structured result as JSON to stdout. Example:

    uv run pdog-image --prompt "提示词" --size 2048x1152 --output ./out.png
    uv run pdog-image --prompt "..." --4k --output ./4k.png
    uv run pdog-image --prompt "..." --image a.png --image b.png   # edit mode
    uv run pdog-image --task-status <task_id> --wait 30
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import (
    FOUR_K_SIZES,
    VALID_ASYNC_MODES,
    VALID_BACKGROUNDS,
    VALID_MODELS,
    VALID_QUALITIES,
    VALID_RESOLUTIONS,
    VALID_SIZES,
    VALID_TRANSPARENT_STRATEGIES,
    PdogError,
    GenRequest,
    collect_async_result,
    config_secret_values,
    load_config,
    mask_key,
    run_generation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdog-image",
        description="Generate or edit images with Pdog (GPT Image 2.5 default, GPT Image 2 compatible).",
    )
    parser.add_argument("--prompt", help="Prompt describing image generation or edit instructions.")
    parser.add_argument(
        "--prompt-file", type=Path, default=None, metavar="PATH",
        help="Read the prompt from a UTF-8 file instead of --prompt. Use this whenever the "
             "prompt contains quotes, $, backticks, or other shell metacharacters.",
    )
    parser.add_argument(
        "--model", choices=VALID_MODELS, default="auto",
        help="'auto' (default; generation->flare, edit->sunburst), flare, sunburst, or image-2.",
    )
    parser.add_argument(
        "--image", action="append",
        help="Reference image path, URL, or data URL for edit mode. Can be specified multiple times.",
    )
    parser.add_argument(
        "--mask",
        help="Mask image for inpainting (transparent area indicates edit zone). Only for explicit mask requests.",
    )
    parser.add_argument("--resolution", choices=VALID_RESOLUTIONS, default=None)
    parser.add_argument("--4k", dest="is_4k", action="store_true", help="Explicit 4K request (4K key + async + flare).")
    parser.add_argument("--size", choices=VALID_SIZES, default=None)
    parser.add_argument("--quality", choices=VALID_QUALITIES, default=None)
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--background", choices=VALID_BACKGROUNDS, default=None)
    parser.add_argument("--transparent-strategy", choices=VALID_TRANSPARENT_STRATEGIES, default="auto")
    parser.add_argument("--key-color", default="auto")
    parser.add_argument("--chroma-tolerance", type=float, default=30.0)
    parser.add_argument("--chroma-feather", type=float, default=40.0)
    parser.add_argument("--chroma-despill-distance", type=float, default=55.0)
    parser.add_argument("--chroma-despill-radius", type=int, default=6)
    parser.add_argument("--keep-keyed", action="store_true")
    parser.add_argument("--async", dest="async_mode", choices=VALID_ASYNC_MODES, default=None)
    parser.add_argument("--poll-interval", type=float, default=None)
    parser.add_argument("--task-timeout", type=float, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--block-seconds", type=float, default=None,
                        help="For async tasks: max seconds to wait inline before returning the pending task id.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--task-status", dest="task_status", default=None, metavar="TASK_ID",
        help="Query a previously submitted async task instead of generating.",
    )
    parser.add_argument("--wait", type=float, default=0.0, help="Seconds to wait when using --task-status.")
    return parser


def _read_prompt(args: argparse.Namespace) -> str:
    """Resolve the prompt from --prompt or --prompt-file, enforcing exactly one."""
    if args.prompt and args.prompt_file:
        raise PdogError(
            "Pass either --prompt or --prompt-file, not both.",
            kind="invalid_parameter",
        )
    if args.prompt_file:
        try:
            text = args.prompt_file.expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise PdogError(
                f"Could not read prompt file {args.prompt_file}: {exc}",
                kind="invalid_parameter",
            ) from exc
        if text.startswith("\ufeff"):
            text = text[1:]
        return text.strip()
    return (args.prompt or "").strip()


def _sanitize(text: str, config: dict[str, Any]) -> str:
    for secret in config_secret_values(config):
        if secret:
            text = text.replace(secret, mask_key(secret))
    return text


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config: dict[str, Any] = {}
    try:
        config = load_config(args.config)
        if args.task_status:
            result = collect_async_result(config, args.task_status, None, wait_seconds=args.wait)
        else:
            prompt = _read_prompt(args)
            if not prompt:
                raise PdogError("Prompt must not be empty.", kind="invalid_parameter")
            # CLI usage is inherently explicit (parity with the skill script),
            # so any 4K tier reached from the command line is allowed.
            request = GenRequest(
                prompt=prompt,
                images=list(args.image or []),
                mask=args.mask,
                size=args.size,
                is_4k=args.is_4k,
                resolution=args.resolution,
                quality=args.quality,
                n=args.n,
                model=args.model,
                background=args.background,
                transparent_strategy=args.transparent_strategy,
                key_color=args.key_color,
                chroma_tolerance=args.chroma_tolerance,
                chroma_feather=args.chroma_feather,
                chroma_despill_distance=args.chroma_despill_distance,
                chroma_despill_radius=args.chroma_despill_radius,
                keep_keyed=args.keep_keyed,
                async_mode=args.async_mode,
                poll_interval=args.poll_interval,
                task_timeout=args.task_timeout,
                output=str(args.output) if args.output else None,
                timeout=args.timeout,
                retries=args.retries,
                dry_run=args.dry_run,
                allow_4k=True,
                block_seconds=args.block_seconds,
            )
            result = run_generation(request, config)
    except PdogError as exc:
        payload = exc.to_dict()
        payload["message"] = _sanitize(str(payload.get("message", "")), config)
        print(json.dumps({"ok": False, "error": payload}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
