"""Pdog image engine: config, routing, and HTTP pipelines.

Extracted from the pdog-gpt-image2 skill's generate_image.py so that the CLI
(`pdog-image`) and the MCP server (`pdog-image-mcp`) share one implementation.

Differences from the skill script (behavior-preserving otherwise):
- failures raise PdogError with structured fields instead of SystemExit;
- engine logs go to stderr (stdout is the MCP stdio protocol channel);
- everything is callable programmatically and returns plain dicts.
"""

from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import os
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, unquote_to_bytes, urlparse
from urllib.request import Request, urlopen

from .errors import PdogError, log
from .netlayer import install_opener
from .transparency import has_real_transparency, verify_transparency

install_opener()

SKILL_CONFIG_PATH = Path.home() / ".zcode" / "skills" / "pdog-gpt-image2" / "config.json"
PROJECT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
# Machine-stable key store: survives plugin updates, marketplace snapshot
# refreshes (which drop gitignored files) and skill-directory changes.
USER_CONFIG_PATH = Path.home() / ".pdog-image" / "config.json"

VALID_SIZES = (
    "auto",
    # 1K tier
    "1024x1024",
    "1280x720",
    "720x1280",
    "1536x1024",
    "1024x1536",
    "1824x1024",
    "1024x1824",
    "1024x768",
    "768x1024",
    "1344x1024",
    "1024x1344",
    "1680x720",
    "1792x768",
    "768x1792",
    # 2K tier
    "2048x2048",
    "2048x1360",
    "1360x2048",
    "2048x1152",
    "1152x2048",
    "2048x1536",
    "1536x2048",
    "2048x896",
    "896x2048",
    # 4K tier
    "2880x2880",
    "3520x2336",
    "2336x3520",
    "3840x2160",
    "2160x3840",
    "3312x2480",
    "2480x3312",
    "3840x1648",
    "1648x3840",
    "4096x4096",
)
VALID_QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")
VALID_RESOLUTIONS = ("1k", "2k", "4k")
VALID_TRANSPARENT_STRATEGIES = ("auto", "native", "chroma-key")
VALID_MODELS = ("auto", "flare", "sunburst", "image-2")
VALID_ASYNC_MODES = ("auto", "always", "never")
VALID_BACKGROUNDS = ("transparent", "white", "black", "auto")

# Sizes whose largest edge exceeds 2048 px require the paid 4K account group.
FOUR_K_SIZES = {
    "2880x2880", "3520x2336", "2336x3520", "3840x2160", "2160x3840",
    "3312x2480", "2480x3312", "3840x1648", "1648x3840", "4096x4096",
}

OnEvent = Callable[[dict], None]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PdogError(f"Configuration file not found: {path}", kind="config") from exc
    except json.JSONDecodeError as exc:
        raise PdogError(f"Configuration file is not valid JSON: {path}", kind="config") from exc
    if not isinstance(config, dict):
        raise PdogError("Configuration must be a JSON object.", kind="config")
    return config


def load_config(explicit: str | os.PathLike | None = None) -> dict[str, Any]:
    """Load the Pdog configuration.

    Search order: explicit path > $PDOG_CONFIG > the user-level
    ~/.pdog-image/config.json (machine-stable key store) > <plugin>/config.json
    > the legacy pdog-gpt-image2 skill's config.json. Environment variables
    PDOG_BASE_URL / PDOG_API_KEY_1K / PDOG_API_KEY_4K / PDOG_API_KEY override
    the corresponding fields, so deployments can keep secrets out of files.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_path = os.environ.get("PDOG_CONFIG")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(USER_CONFIG_PATH)
    # The plugin directory is last: a config left there is fine, but it must
    # not shadow the machine-stable user-level key store.
    candidates.append(PROJECT_CONFIG_PATH)
    candidates.append(SKILL_CONFIG_PATH)

    config: dict[str, Any] | None = None
    for candidate in candidates:
        if candidate.is_file():
            config = _read_config_file(candidate)
            break
    if config is None:
        searched = ", ".join(str(p) for p in candidates)
        raise PdogError(
            f"No Pdog configuration found (searched: {searched}).",
            kind="config",
            hint="Copy config.example.json to config.json and fill in your keys.",
        )

    # A routes-based config carries its own base_url/model per route, so the
    # top-level fields are only required by the legacy flat layout.
    has_routes = isinstance(config.get("routes"), (list, dict)) and bool(config.get("routes"))
    if not has_routes:
        if not isinstance(config.get("base_url"), str) or not config["base_url"].strip():
            raise PdogError("Configuration is missing a non-empty 'base_url' value.", kind="config")
        if not isinstance(config.get("model"), str) or not config["model"].strip():
            raise PdogError("Configuration is missing a non-empty 'model' value.", kind="config")

    if os.environ.get("PDOG_BASE_URL"):
        config["base_url"] = os.environ["PDOG_BASE_URL"].strip()
        for route in config["routes"] if isinstance(config.get("routes"), list) else []:
            if isinstance(route, dict) and not route.get("base_url"):
                route["base_url"] = config["base_url"]
    api_keys = config.setdefault("api_keys", {})
    if not isinstance(api_keys, dict):
        api_keys = {}
        config["api_keys"] = api_keys
    for slot, var in (("1k", "PDOG_API_KEY_1K"), ("4k", "PDOG_API_KEY_4K")):
        if os.environ.get(var):
            api_keys[slot] = os.environ[var].strip()
    if os.environ.get("PDOG_API_KEY"):
        config["api_key"] = os.environ["PDOG_API_KEY"].strip()
    return config


# ---------------------------------------------------------------------------
# Routes: per-tier endpoints/keys with ordered fallback
# ---------------------------------------------------------------------------

# A route is one credential+endpoint pair: `base_url`, keys per tier, and the
# models it serves for generation and editing. Requests resolve to an ordered
# list of routes for their tier; if the first fails in a way that cannot have
# been billed, the next one is tried.

@dataclass(frozen=True)
class Route:
    """One upstream endpoint with its keys and models."""

    name: str
    base_url: str
    api_key_1k: str = ""
    api_key_4k: str = ""
    model_generation: str = ""
    model_edit: str = ""
    model_generation_4k: str = ""
    model_edit_4k: str = ""
    # Additional model IDs this route accepts when a caller names one
    # explicitly (legacy configs list them in `models`).
    extra_models: Tuple[str, ...] = ()
    # Tiers this route is eligible for; empty means every tier.
    tiers: Tuple[str, ...] = ()

    def key_for(self, tier: str) -> str:
        return self.api_key_4k if tier == "4k" else self.api_key_1k

    def model_for(self, tier: str, edit_mode: bool) -> str:
        """Concrete model for this tier/mode, falling back along the chain.

        A route that only declares a generation model still serves edits with
        it, which matches how the legacy single-model config behaved.
        """
        four_k = tier == "4k"
        if edit_mode:
            candidates = (
                (self.model_edit_4k, self.model_generation_4k, self.model_edit, self.model_generation)
                if four_k
                else (self.model_edit, self.model_generation)
            )
        else:
            candidates = (
                (self.model_generation_4k, self.model_edit_4k, self.model_generation, self.model_edit)
                if four_k
                else (self.model_generation, self.model_edit)
            )
        for candidate in candidates:
            if candidate:
                return candidate
        return ""

    def accepted_models(self, tier: str) -> Tuple[str, ...]:
        """Model IDs a caller may request explicitly on this tier.

        A route that declares tier-specific models restricts that tier to them;
        otherwise the tier inherits the general models. `extra_models` always
        applies, which is how legacy configs keep accepting every alias they
        list in `models`.
        """
        if tier == "4k" and (self.model_generation_4k or self.model_edit_4k):
            base = (self.model_generation_4k, self.model_edit_4k)
        else:
            base = (
                self.model_generation,
                self.model_edit,
                self.model_generation_4k,
                self.model_edit_4k,
            )
        merged = list(base) + list(self.extra_models)
        return tuple(dict.fromkeys(model for model in merged if model))

    def serves(self, tier: str) -> bool:
        return not self.tiers or tier in self.tiers

    def keys(self) -> list[str]:
        return [key for key in (self.api_key_1k, self.api_key_4k) if key]


def _first_str(*candidates: Any) -> str:
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _route_from_mapping(
    name: str,
    raw: dict[str, Any],
    *,
    config: dict[str, Any],
) -> Optional[Route]:
    """Build one Route from a config object.

    Supported shapes (all optional except base_url somewhere):
      {"base_url", "api_key" | "api_keys": {"1k","4k"}, "tiers": [...]}
      {"model", "generation", "edit", "generation_4k", "edit_4k"}
    """
    if not isinstance(raw, dict):
        return None

    base_url = raw.get("base_url") or config.get("base_url") or ""
    if not isinstance(base_url, str) or not base_url.strip():
        return None

    keys = raw.get("api_keys") if isinstance(raw.get("api_keys"), dict) else {}
    # A single `api_key` covers both tiers, which is what the legacy flat config
    # meant; an explicit api_keys slot always wins over it.
    key_1k = _first_str(keys.get("1k"), raw.get("api_key_1k"), raw.get("api_key"))
    key_4k = _first_str(keys.get("4k"), raw.get("api_key_4k"), raw.get("api_key"))

    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}

    generation = _first_str(
        raw.get("generation"), raw.get("model_generation"), models.get("generation")
    )
    edit = _first_str(raw.get("edit"), raw.get("model_edit"), models.get("edit"))
    generation_4k = _first_str(
        raw.get("generation_4k"),
        raw.get("model_generation_4k"),
        models.get("generation_4k"),
    )
    edit_4k = _first_str(raw.get("edit_4k"), raw.get("model_edit_4k"), models.get("edit_4k"))

    tiers_raw = raw.get("tiers")
    tiers: Tuple[str, ...] = ()
    if isinstance(tiers_raw, (list, tuple)):
        tiers = tuple(
            str(item).lower() for item in tiers_raw if str(item).lower() in VALID_RESOLUTIONS
        )

    extra_models = tuple(
        value.strip()
        for value in models.values()
        if isinstance(value, str) and value.strip()
    )

    return Route(
        name=name,
        base_url=base_url.rstrip("/"),
        api_key_1k=key_1k,
        api_key_4k=key_4k,
        model_generation=generation,
        model_edit=edit,
        model_generation_4k=generation_4k,
        model_edit_4k=edit_4k,
        extra_models=extra_models,
        tiers=tiers,
    )


def _legacy_route(config: dict[str, Any]) -> Optional[Route]:
    """Translate the pre-routes flat config into a single Route.

    The flat layout kept one base_url with per-tier keys and model aliases in
    `models` / `model_auto_routing`, so all of that has to be flattened into one
    concrete route here.
    """
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    routing = (
        config.get("model_auto_routing")
        if isinstance(config.get("model_auto_routing"), dict)
        else {}
    )
    default_alias = config.get("model_default_alias", "flare")

    def alias_model(key: str, fallback: str = "") -> str:
        alias = routing.get(key) or fallback or default_alias
        if isinstance(alias, str) and alias in models:
            resolved = models.get(alias)
            if isinstance(resolved, str) and resolved.strip():
                return resolved.strip()
        if isinstance(alias, str) and alias.strip():
            return alias.strip()
        return ""

    generation = alias_model("generation", default_alias)
    edit = alias_model("edit", default_alias)
    generation_4k = alias_model("generation_4k", routing.get("generation") or default_alias)
    edit_4k = alias_model("edit_4k", routing.get("edit") or default_alias)

    # A flat config with no explicit per-mode models still gets the global
    # `model` value so it behaves exactly as before.
    global_model = config.get("model")
    if not isinstance(global_model, str) or not global_model.strip():
        global_model = ""
    else:
        global_model = global_model.strip()
    if not generation:
        generation = global_model
    if not edit:
        edit = global_model

    api_keys = config.get("api_keys") if isinstance(config.get("api_keys"), dict) else {}
    return _route_from_mapping(
        "default",
        {
            "base_url": config.get("base_url"),
            "api_keys": api_keys,
            "api_key": config.get("api_key"),
            "generation": generation,
            "edit": edit,
            "generation_4k": generation_4k,
            "edit_4k": edit_4k,
        },
        config=config,
    )


def normalize_routes(config: dict[str, Any]) -> List[Route]:
    """Return the effective route list, primary first.

    `routes` in config may be a list of route objects (ordered fallback) or a
    mapping of name -> route object. When absent, the legacy flat config is
    translated into a single route so old configs keep working unchanged.
    """
    raw_routes = config.get("routes")
    routes: List[Route] = []

    if isinstance(raw_routes, list):
        for index, item in enumerate(raw_routes):
            if not isinstance(item, dict):
                continue
            name = _first_str(item.get("name")) or f"route-{index + 1}"
            route = _route_from_mapping(name, item, config=config)
            if route is not None:
                routes.append(route)
    elif isinstance(raw_routes, dict):
        for name, item in raw_routes.items():
            if not isinstance(item, dict):
                continue
            route = _route_from_mapping(str(name), item, config=config)
            if route is not None:
                routes.append(route)

    if not routes:
        legacy = _legacy_route(config)
        if legacy is not None:
            routes.append(legacy)
    return routes


def routes_for_tier(config: dict[str, Any], tier: str) -> List[Route]:
    """Eligible routes for a tier, in configured fallback order."""
    return [route for route in normalize_routes(config) if route.serves(tier)]


def config_secret_values(config: dict[str, Any]) -> list[str]:
    """All configured API keys across every route, for scrubbing error text."""
    values: list[str] = []
    for route in normalize_routes(config):
        values.extend(route.keys())
    return values


def mask_key(key: str) -> str:
    if len(key) <= 12:
        return "****"
    return f"{key[:6]}...{key[-4:]}"


def response_message(body: bytes) -> str:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body.decode("utf-8", errors="replace")[:500]
    if isinstance(decoded, dict):
        error = decoded.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
        if "detail" in decoded and isinstance(decoded["detail"], str):
            return decoded["detail"]
    return json.dumps(decoded, ensure_ascii=False)[:500]


# ---------------------------------------------------------------------------
# Endpoints, multipart, PNG header
# ---------------------------------------------------------------------------

def get_endpoint(base_url: str, edit_mode: bool = False) -> str:
    clean_url = base_url.rstrip("/")
    operation = "edits" if edit_mode else "generations"
    if not clean_url.endswith("/v1"):
        return f"{clean_url}/v1/images/{operation}"
    return f"{clean_url}/images/{operation}"


def get_async_endpoint(base_url: str, edit_mode: bool = False) -> str:
    return get_endpoint(base_url, edit_mode=edit_mode) + "/async"


def encode_multipart(
    fields: dict[str, Any],
    images: List[Tuple[str, str, bytes]],
    mask: Optional[Tuple[str, str, bytes]] = None,
) -> Tuple[bytes, str]:
    """Encode edit fields and binary image inputs as multipart/form-data."""
    boundary = f"----pdog-{secrets.token_hex(16)}"
    chunks: List[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    for filename, mime, content in images:
        safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="image"; filename="{safe_filename}"\r\n'.encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode("ascii"))
        chunks.append(content)
        chunks.append(b"\r\n")

    if mask:
        filename, mime, content = mask
        safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="mask"; filename="{safe_filename}"\r\n'.encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode("ascii"))
        chunks.append(content)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def png_dimensions(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    signature = b"\x89PNG\r\n\x1a\n"
    if len(image_bytes) >= 24 and image_bytes[:8] == signature and image_bytes[12:16] == b"IHDR":
        return struct.unpack(">II", image_bytes[16:24])
    return None


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def resolve_tier(
    config: dict[str, Any],
    *,
    is_4k: bool = False,
    resolution: Optional[str] = None,
    size: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Determine (tier, api_key, effective_size).

    Default is 1k unless 4k is explicitly requested via is_4k, resolution=4k,
    or a 4K-specific size. 2K shares the regular 1K key.
    """
    if size is not None and size not in VALID_SIZES:
        raise PdogError(
            f"Invalid size '{size}'. Valid sizes: {', '.join(VALID_SIZES)}.",
            kind="invalid_parameter",
        )
    if resolution is not None and resolution not in VALID_RESOLUTIONS:
        raise PdogError(
            f"Invalid resolution '{resolution}'. Valid values: {', '.join(VALID_RESOLUTIONS)}.",
            kind="invalid_parameter",
        )

    # 1. Determine resolution tier
    if is_4k or resolution == "4k":
        mode = "4k"
    elif resolution in ("1k", "2k"):
        mode = resolution
    elif size in FOUR_K_SIZES:
        mode = "4k"
    elif size == "auto" or size is None:
        mode = str(config.get("default_resolution", "1k")).lower()
        if mode not in ("1k", "2k", "4k"):
            mode = "1k"
    else:
        # Infer the tier from the requested dimensions: <=1K edge, 2K, or larger.
        try:
            width, height = (int(part) for part in size.lower().split("x", 1))
            largest = max(width, height)
        except (TypeError, ValueError, AttributeError):
            largest = 0
        if largest > 2048:
            mode = "4k"
        elif largest > 1536:
            mode = "2k"
        else:
            mode = "1k"

    # 2. Pick the key from the first route that serves this tier
    key = ""
    for route in routes_for_tier(config, mode):
        candidate = route.key_for(mode)
        if candidate:
            key = candidate
            break
    if not key:
        # Legacy configs may still carry a bare `api_key` with no routes.
        fallback_key = config.get("api_key", "")
        if isinstance(fallback_key, str) and fallback_key.strip():
            key = fallback_key.strip()

    if not key:
        raise PdogError(
            f"No API key configured for resolution mode '{mode}'.",
            kind="config",
            hint="Configure `routes` (or api_keys) in config.json, or set PDOG_API_KEY_*.",
        )

    # 3. Determine target size
    if size:
        final_size = size
    else:
        final_size = "3840x2160" if mode == "4k" else "2048x1152"

    if mode == "4k" and final_size != "auto":
        try:
            width, height = (int(part) for part in final_size.lower().split("x", 1))
        except (TypeError, ValueError):
            width = height = 0
        if max(width, height) <= 2048:
            raise PdogError(
                "Refusing a 2K-sized request on the 4K paid route. "
                "Use the regular 1K/2K route for 2048-class output, or request a true 4K size.",
                kind="cost_guard",
            )

    return mode, key, final_size


def resolve_model(
    config: dict[str, Any],
    *,
    model: str = "auto",
    edit_mode: bool,
    tier: str,
    route: Optional[Route] = None,
) -> Tuple[str, str]:
    """Resolve the concrete model ID and a human-readable reason.

    'auto' takes the route's configured generation/edit model for the tier.
    When the config has no `routes`, the legacy `models` / `model_auto_routing`
    aliases are honoured so existing configs resolve to the same model IDs.

    `route` may name the route to resolve against; without it the first route
    that serves `tier` is used.
    """
    if model not in VALID_MODELS:
        raise PdogError(
            f"Invalid model '{model}'. Valid values: {', '.join(VALID_MODELS)}.",
            kind="invalid_parameter",
        )

    models = config.get("models", {}) if isinstance(config.get("models"), dict) else {}
    resolved_route = route
    if resolved_route is None:
        candidates = routes_for_tier(config, tier)
        resolved_route = candidates[0] if candidates else None
    if resolved_route is None:
        raise PdogError(
            f"No route is configured for resolution mode '{tier}'.",
            kind="config",
            hint="Add a `routes` entry covering this tier, or set base_url/api_keys.",
        )

    if model == "auto":
        reason = (
            f"auto-4k-{'edit' if edit_mode else 'generation'}"
            if tier == "4k"
            else ("auto-edit" if edit_mode else "auto-generation")
        )
        resolved = resolved_route.model_for(tier, edit_mode)
    else:
        reason = f"explicit({model})"
        # An explicit request may be an alias (legacy configs) or a real model
        # ID; resolve aliases first, then accept the literal value.
        resolved = ""
        alias_value = models.get(model)
        if isinstance(alias_value, str) and alias_value.strip():
            resolved = alias_value.strip()
        elif model in resolved_route.accepted_models(tier) or not models:
            if model in VALID_MODELS:
                # Legacy aliases map through `models`; when a route declares its
                # own model IDs, an alias with no mapping cannot be honoured.
                resolved = alias_value.strip() if isinstance(alias_value, str) else ""
        if not resolved:
            aliases = resolved_route.accepted_models(tier)
            if model in aliases:
                resolved = model
            else:
                raise PdogError(
                    f"Model '{model}' is not configured on route '{resolved_route.name}' "
                    f"for tier '{tier}'. Available: {', '.join(aliases) or '(none)'}.",
                    kind="invalid_parameter",
                    route=resolved_route.name,
                )

    if not isinstance(resolved, str) or not resolved.strip():
        raise PdogError("No usable model ID resolved from configuration.", kind="config")
    resolved = resolved.strip()

    if tier == "4k" and resolved == models.get("sunburst"):
        raise PdogError(
            "Model 'gpt-image-2.5-sunburst' is not served by the 4K account group on this gateway. "
            "Use model 'flare' (or leave 'auto') for 4K output.",
            kind="invalid_parameter",
        )

    return resolved, reason


def resolve_image_input(value: str, fallback_name: str) -> Tuple[str, str, bytes]:
    """Resolve a local path, URL, or data URL into a (filename, mime, bytes) tuple."""
    source = value.strip()

    if source.startswith("data:"):
        header, separator, encoded = source.partition(",")
        if not separator:
            raise PdogError("Invalid image data URL: missing comma separator.", kind="invalid_parameter")
        metadata = header[5:].split(";")
        mime = metadata[0] or "application/octet-stream"
        try:
            if "base64" in metadata[1:]:
                content = base64.b64decode(encoded, validate=True)
            else:
                content = unquote_to_bytes(encoded)
        except (binascii.Error, ValueError) as exc:
            raise PdogError(f"Invalid image data URL: {exc}", kind="invalid_parameter") from exc
        extension = mimetypes.guess_extension(mime) or ".bin"
        return f"{fallback_name}{extension}", mime, content

    if source.startswith("http://") or source.startswith("https://"):
        request = Request(source, headers={"User-Agent": "Pdog-Image-Client/1.0"})
        try:
            with urlopen(request, timeout=120) as response:
                content = response.read()
                mime = response.headers.get_content_type()
        except (HTTPError, URLError, TimeoutError, socket.timeout) as exc:
            raise PdogError(f"Could not download reference image: {exc}", kind="network") from exc
        filename = Path(unquote(urlparse(source).path)).name or fallback_name
        if not Path(filename).suffix:
            filename += mimetypes.guess_extension(mime) or ".bin"
        return filename, mime, content

    path = Path(source).expanduser().resolve()
    if not path.is_file():
        raise PdogError(f"Reference image file not found: {source}", kind="invalid_parameter")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.name, mime, path.read_bytes()


# ---------------------------------------------------------------------------
# HTTP calls
# ---------------------------------------------------------------------------

def request_image(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    images: Optional[List[Tuple[str, str, bytes]]] = None,
    mask: Optional[Tuple[str, str, bytes]] = None,
    timeout_seconds: float = 300.0,
    max_retries: int = 2,
) -> bytes:
    edit_mode = bool(images)
    endpoint = get_endpoint(base_url, edit_mode=edit_mode)
    headers = {"Authorization": f"Bearer {api_key}"}
    if edit_mode:
        data_bytes, content_type = encode_multipart(payload, images or [], mask)
        headers["Content-Type"] = content_type
    else:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    for attempt in range(max_retries + 1):
        request = Request(endpoint, data=data_bytes, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            status = exc.code
            msg = response_message(exc.read())
            if status in (502, 503, 504) and attempt < max_retries:
                backoff = 2 ** attempt
                log(f"Received HTTP {status}, retrying in {backoff}s... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(backoff)
                continue
            raise PdogError(
                f"Image API returned HTTP {status}: {msg}",
                kind="http",
                http_status=status,
                hint=(
                    "网关当前账户组未配置该模型：4K 档可尝试 model='image-2'（gpt-image-2），"
                    "或联系服务商确认账户组的模型配置。"
                    if status == 404 and "not supported" in msg.lower()
                    else "400/401/402/404 不重试：请核对参数、Key 或余额；502/503/504 可稍后重试。"
                ),
            ) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            if attempt < max_retries:
                backoff = 2 ** attempt
                log(f"Network error ({exc}), retrying in {backoff}s... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(backoff)
                continue
            # A timeout after the request was sent may already have been billed
            # upstream, so another route must not silently resubmit it. A
            # connection that never opened cannot have produced an image.
            timed_out = isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
                getattr(exc, "reason", None), (TimeoutError, socket.timeout)
            )
            raise PdogError(
                f"Could not reach Image API: {exc}",
                kind="network",
                failover_safe=not timed_out,
                hint=(
                    "请求已发出但超时，上游可能已计费；换路由重试可能重复扣费，请先确认。"
                    if timed_out
                    else None
                ),
            ) from exc

    raise PdogError("Image API request failed after retries.", kind="network")


def download_image_url(url: str, api_key: Optional[str] = None, referer: Optional[str] = None) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 Pdog-Image-Client/1.2",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    if api_key and "X-Amz-" not in url and "amazonaws.com" not in url:
        headers["Authorization"] = f"Bearer {api_key}"
    if referer:
        headers["Referer"] = referer.rstrip("/") + "/"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=120) as response:
            return response.read()
    except HTTPError as exc:
        raise PdogError(
            "Image API returned a download URL, but the file server rejected it with "
            f"HTTP {exc.code}. The generation response was saved locally; retry the download "
            "instead of regenerating when possible.",
            kind="http",
            http_status=exc.code,
        ) from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        raise PdogError(f"Could not download generated image: {exc}", kind="network") from exc


def extract_images(
    api_response: bytes, api_key: Optional[str] = None, base_url: Optional[str] = None
) -> List[bytes]:
    try:
        body = json.loads(api_response.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PdogError(
            f"Invalid JSON from API: {api_response[:200]!r}", kind="upstream"
        ) from exc

    results: List[bytes] = []

    # Case 1: Standard OpenAI format { "data": [ { "b64_json": "..." } or { "url": "..." } ] }
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        for item in body["data"]:
            if isinstance(item, dict):
                if item.get("b64_json"):
                    raw = item["b64_json"]
                    if raw.startswith("data:"):
                        raw = raw.partition(",")[2]
                    results.append(base64.b64decode(raw))
                elif item.get("url"):
                    results.append(download_image_url(item["url"], api_key=api_key, referer=base_url))
    # Case 2: Cloud format { "images": [ "https://..." or "data:..." ] }
    elif isinstance(body, dict) and isinstance(body.get("images"), list):
        for item in body["images"]:
            if isinstance(item, str):
                if item.startswith("http://") or item.startswith("https://"):
                    results.append(download_image_url(item, api_key=api_key, referer=base_url))
                elif item.startswith("data:"):
                    results.append(base64.b64decode(item.partition(",")[2]))
                else:
                    results.append(base64.b64decode(item))

    if not results:
        raise PdogError(
            f"Image API did not return image data: {response_message(api_response)}",
            kind="upstream",
        )
    return results


def submit_async_task(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    images: Optional[List[Tuple[str, str, bytes]]] = None,
    mask: Optional[Tuple[str, str, bytes]] = None,
    timeout_seconds: float = 300.0,
    max_retries: int = 2,
) -> str:
    """Submit an async image task and return its task id."""
    edit_mode = bool(images)
    endpoint = get_async_endpoint(base_url, edit_mode=edit_mode)
    headers = {"Authorization": f"Bearer {api_key}"}
    if edit_mode:
        data_bytes, content_type = encode_multipart(payload, images or [], mask)
        headers["Content-Type"] = content_type
    else:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    raw = None
    for attempt in range(max_retries + 1):
        request = Request(endpoint, data=data_bytes, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
            break
        except HTTPError as exc:
            status = exc.code
            msg = response_message(exc.read())
            if status in (502, 503, 504) and attempt < max_retries:
                backoff = 2 ** attempt
                log(f"Async submit HTTP {status}, retrying in {backoff}s... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(backoff)
                continue
            if status == 404:
                raise PdogError(
                    f"Async submit returned HTTP 404: {msg}",
                    kind="http",
                    http_status=404,
                    hint="异步存储可能未开启：改用 async_mode='never' 走同步接口。",
                ) from exc
            raise PdogError(
                f"Async submit returned HTTP {status}: {msg}", kind="http", http_status=status
            ) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            if attempt < max_retries:
                backoff = 2 ** attempt
                log(f"Async submit network error ({exc}), retrying in {backoff}s...")
                time.sleep(backoff)
                continue
            raise PdogError(f"Could not reach async endpoint: {exc}", kind="network") from exc
    if raw is None:
        raise PdogError("Async submit failed after retries.", kind="network")

    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PdogError(f"Invalid JSON from async submit: {raw[:200]!r}", kind="upstream") from exc
    task_id = body.get("task_id") or body.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise PdogError(
            f"Async submit did not return a task id: {response_message(raw)}", kind="upstream"
        )
    return task_id


def poll_async_status_once(base_url: str, api_key: str, task_id: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/images/tasks/{task_id}"
    request = Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urlopen(request, timeout=120) as response:
            raw = response.read()
    except HTTPError as exc:
        raise PdogError(
            f"Async poll for task {task_id} returned HTTP {exc.code}: "
            f"{response_message(exc.read())}",
            kind="http",
            http_status=exc.code,
            hint=f"任务已提交并计费，稍后用 pdog_task_status(task_id='{task_id}') 继续查询，切勿重复提交。",
        ) from exc
    except (URLError, TimeoutError, socket.timeout) as exc:
        raise PdogError(
            f"Async poll for task {task_id} hit a network error: {exc}",
            kind="network",
            hint=f"任务已提交并计费，稍后用 pdog_task_status(task_id='{task_id}') 继续查询，切勿重复提交。",
        ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PdogError(
            f"Invalid JSON from async poll for task {task_id}: {raw[:200]!r}",
            kind="upstream",
            hint=f"任务已提交并计费，稍后用 pdog_task_status(task_id='{task_id}') 继续查询。",
        ) from exc


def _task_image_urls(body: dict[str, Any]) -> List[str]:
    urls: List[str] = []
    image_url = body.get("image_url")
    if isinstance(image_url, str) and image_url:
        urls.append(image_url)
    result = body.get("result")
    if isinstance(result, dict):
        for item in result.get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"] not in urls:
                urls.append(item["url"])
    return urls


def poll_async_task(
    base_url: str,
    api_key: str,
    task_id: str,
    *,
    poll_interval: float = 5.0,
    task_timeout: float = 1800.0,
    block_seconds: Optional[float] = None,
    on_event: Optional[OnEvent] = None,
) -> dict[str, Any]:
    """Poll an async task.

    Returns {"state": "done", "images": [...]} once completed, or
    {"state": "pending", "status": ...} when block_seconds elapsed without
    completion (the task keeps running server-side; poll again later — never
    resubmit, the task is already billed).
    """
    start = time.monotonic()
    last_status = "unknown"
    while True:
        elapsed = time.monotonic() - start
        if elapsed > task_timeout:
            raise PdogError(
                f"Async task {task_id} did not finish within {int(task_timeout)}s "
                f"(last status: {last_status}).",
                kind="upstream",
                hint="任务仍在服务端运行，请稍后用 pdog_task_status(task_id) 继续查询，切勿重复提交。",
            )
        body = poll_async_status_once(base_url, api_key, task_id)
        status = body.get("status") or "unknown"
        last_status = str(status)
        if on_event:
            on_event({
                "type": "poll",
                "task_id": task_id,
                "status": last_status,
                "elapsed": round(time.monotonic() - start, 1),
                "total": round(task_timeout, 1),
            })
        if status == "completed":
            urls = _task_image_urls(body)
            if not urls:
                raise PdogError(
                    f"Async task {task_id} completed without an image URL: "
                    f"{json.dumps(body, ensure_ascii=False)[:300]}",
                    kind="upstream",
                )
            images = [download_image_url(u, api_key=api_key, referer=base_url) for u in urls]
            return {"state": "done", "images": images, "status": "completed"}
        if status == "failed":
            error = body.get("error")
            error_text = (
                json.dumps(error, ensure_ascii=False) if error else json.dumps(body, ensure_ascii=False)[:300]
            )
            hint = None
            lowered = error_text.lower()
            if "model_not_found" in error_text or "not supported" in lowered:
                hint = (
                    "网关异步工作组未配置该模型；本网关 4K 账户组当前对 2.5 模型族"
                    "（flare/sunburst）同步与异步均返回 model_not_found，"
                    "可尝试 model='image-2' 或联系服务商确认账户组配置。"
                )
            elif "no available compatible accounts" in lowered:
                hint = (
                    "网关侧该档位账户组暂时没有可用资源（不是本地配置或参数错误）。"
                    "稍后重试，或联系服务商确认 4K 档账户组是否在线；"
                    "也可先用 1K/2K 档出图，稍后再补 4K。"
                )
            elif "内容政策" in error_text or "content policy" in lowered:
                hint = (
                    "上游内容审核拒绝了该提示词。换更中性的描述重试；"
                    "若中性提示词也被拒，多为该账户组审核策略问题，需联系服务商。"
                )
            raise PdogError(
                f"Async task {task_id} failed: {error_text}",
                kind="upstream",
                hint=hint,
            )
        if block_seconds is not None and (time.monotonic() - start) >= block_seconds:
            return {"state": "pending", "status": last_status}
        time.sleep(max(poll_interval, 0.5))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class GenRequest:
    """One generation/edit request, transport-agnostic (CLI and MCP both build this)."""

    prompt: str
    images: List[str] = field(default_factory=list)
    mask: Optional[str] = None
    size: Optional[str] = None
    is_4k: bool = False
    resolution: Optional[str] = None
    quality: Optional[str] = None
    n: int = 1
    model: str = "auto"
    background: Optional[str] = None
    transparent_strategy: str = "auto"
    key_color: str = "auto"
    chroma_tolerance: float = 30.0
    chroma_feather: float = 40.0
    chroma_despill_distance: float = 55.0
    chroma_despill_radius: int = 6
    keep_keyed: bool = False
    async_mode: Optional[str] = None
    poll_interval: Optional[float] = None
    task_timeout: Optional[float] = None
    output: Optional[str] = None
    timeout: Optional[float] = None
    retries: int = 2
    dry_run: bool = False
    allow_4k: bool = False
    block_seconds: Optional[float] = None


def default_output(index: int = 0, total: int = 1) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{index + 1}" if total > 1 else ""
    return Path.cwd() / "output" / f"pdog-image-{timestamp}{suffix}.png"


def _validate_common(req: GenRequest) -> None:
    if not req.prompt or not req.prompt.strip():
        raise PdogError("Prompt must not be empty.", kind="invalid_parameter")
    if not 1 <= req.n <= 10:
        raise PdogError("n must be between 1 and 10.", kind="invalid_parameter")
    if req.retries < 0:
        raise PdogError("retries must be zero or greater.", kind="invalid_parameter")
    if req.timeout is not None and req.timeout <= 0:
        raise PdogError("timeout must be greater than zero.", kind="invalid_parameter")
    if req.chroma_tolerance < 0 or req.chroma_feather <= 0:
        raise PdogError(
            "Chroma tolerance must be non-negative and feather must be positive.",
            kind="invalid_parameter",
        )
    if req.chroma_despill_distance < 0 or req.chroma_despill_radius < 0:
        raise PdogError(
            "Chroma despill distance and radius must be non-negative.", kind="invalid_parameter"
        )
    if req.background is not None and req.background not in VALID_BACKGROUNDS:
        raise PdogError(
            f"Invalid background '{req.background}'. Valid values: {', '.join(VALID_BACKGROUNDS)}.",
            kind="invalid_parameter",
        )
    if req.transparent_strategy not in VALID_TRANSPARENT_STRATEGIES:
        raise PdogError(
            f"Invalid transparent_strategy '{req.transparent_strategy}'. "
            f"Valid values: {', '.join(VALID_TRANSPARENT_STRATEGIES)}.",
            kind="invalid_parameter",
        )


def _finalize_images(
    images_data: List[bytes],
    req: GenRequest,
    *,
    first_output: Path,
    key_color: Optional[Tuple[int, int, int]],
    transport_note: str,
    on_event: Optional[OnEvent] = None,
) -> dict[str, Any]:
    """Save upstream image bytes (with optional chroma post-processing) to disk."""
    transparent_requested = req.background == "transparent"
    chroma_mode = transparent_requested and req.transparent_strategy == "chroma-key"
    total_images = len(images_data)
    files: List[dict[str, Any]] = []
    chroma_metrics: Optional[dict[str, Any]] = None
    transparency_check: Optional[dict[str, Any]] = None

    for idx, upstream_bytes in enumerate(images_data):
        if total_images == 1:
            out_path = first_output
        else:
            out_path = first_output.parent / f"{first_output.stem}-{idx + 1}{first_output.suffix or '.png'}"

        img_bytes = upstream_bytes
        note = "upstream"
        if chroma_mode and key_color is not None:
            from . import chroma as _chroma

            try:
                img_bytes, chroma_metrics = _chroma.extract_edge_connected_key(
                    upstream_bytes,
                    key_color,
                    tolerance=req.chroma_tolerance,
                    feather=req.chroma_feather,
                    despill_distance=req.chroma_despill_distance,
                    despill_radius=req.chroma_despill_radius,
                )
            except PdogError:
                raise
            except ValueError as exc:
                raise PdogError(
                    f"Dynamic chroma-key transparency failed: {exc}",
                    kind="upstream",
                    hint=(
                        "已生成的色键原图不会浪费：可用 pdog_chroma_key 对已保存的 -keyed 图像"
                        "调整参数重试，无需重新生成。"
                    ),
                ) from exc
            note = (
                f"chroma-key #{'%02X%02X%02X' % key_color}, "
                f"transparent {chroma_metrics['transparent_percent']}%, no resize"
            )
        elif transparent_requested and not has_real_transparency(upstream_bytes):
            rejected = out_path.with_name(f"{out_path.stem}-noalpha{out_path.suffix or '.png'}")
            rejected.parent.mkdir(parents=True, exist_ok=True)
            rejected.write_bytes(upstream_bytes)
            raise PdogError(
                "Transparent output requested, but upstream returned no real transparent pixels. "
                "Native transparency is required by auto/native mode; the result was rejected.",
                kind="upstream",
                hint=(
                    f"上游原图已保存到 {rejected} 以免浪费已扣费产出；"
                    "如接受色键回退，请显式使用 transparent_strategy='chroma-key'。"
                ),
            )

        out_path = out_path.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if chroma_mode and req.keep_keyed:
            keyed_path = out_path.with_name(f"{out_path.stem}-keyed{out_path.suffix or '.png'}")
            keyed_path.write_bytes(upstream_bytes)
            dims = png_dimensions(upstream_bytes)
            files.append({
                "path": str(keyed_path),
                "width": dims[0] if dims else None,
                "height": dims[1] if dims else None,
                "bytes": len(upstream_bytes),
                "role": "keyed",
            })
        out_path.write_bytes(img_bytes)

        dims = png_dimensions(img_bytes)
        files.append({
            "path": str(out_path),
            "width": dims[0] if dims else None,
            "height": dims[1] if dims else None,
            "bytes": len(img_bytes),
            "role": "final",
            "transparency": note,
        })
        if on_event:
            on_event({"type": "saved", "path": str(out_path), "index": idx + 1, "total": total_images})

    if transparent_requested and files:
        final_files = [f for f in files if f.get("role") == "final"]
        if final_files:
            with open(final_files[0]["path"], "rb") as handle:
                transparency_check = verify_transparency(handle.read())

    return {
        "files": files,
        "chroma": chroma_metrics,
        "transparency_check": transparency_check,
    }


@dataclass
class _JobOutcome:
    """Result of executing on one route (successful, or pending async task)."""

    images: List[bytes]
    transport: str
    model: str
    model_reason: str
    route: str
    pending_task_id: Optional[str] = None
    pending_status: Optional[str] = None


def _resolve_chroma_key(req: GenRequest, first_image_bytes: bytes) -> Tuple[Tuple[int, int, int], dict]:
    from . import chroma as _chroma

    _chroma._require_available()
    if req.key_color == "auto":
        return _chroma.choose_key_color(first_image_bytes)
    color = _chroma.parse_hex_color(req.key_color)
    return color, {"selected": _chroma.color_hex(color), "scores": None}


def run_generation(
    req: GenRequest,
    config: dict[str, Any],
    *,
    on_event: Optional[OnEvent] = None,
) -> dict[str, Any]:
    """Execute one generation/edit request end to end and return a structured result."""
    _validate_common(req)

    images = [resolve_image_input(image, f"image-{index + 1}") for index, image in enumerate(req.images)]
    mask = resolve_image_input(req.mask, "mask") if req.mask else None
    if mask and not images:
        raise PdogError(
            "mask requires at least one reference image.", kind="invalid_parameter"
        )

    edit_mode = bool(images)
    tier, api_key, final_size = resolve_tier(
        config, is_4k=req.is_4k, resolution=req.resolution, size=req.size
    )

    if tier == "4k" and not req.allow_4k and not req.dry_run:
        raise PdogError(
            "4K 生成走付费 4K 路由（约 ¥0.08/张），必须显式确认。",
            kind="cost_guard",
            hint="用户明确要求 4K 后，携带 allow_4k=true 重新调用本工具。",
        )

    # Fallback chain for this tier. A route with no key for the tier cannot
    # serve it, so it is skipped instead of failing the whole request.
    routes = [route for route in routes_for_tier(config, tier) if route.key_for(tier)]
    if not routes:
        raise PdogError(
            f"No route with an API key is configured for resolution mode '{tier}'.",
            kind="config",
            hint="Add a `routes` entry covering this tier, or set api_keys for it.",
        )
    primary = routes[0]
    api_key = primary.key_for(tier)
    base_url = primary.base_url
    model_id, model_reason = resolve_model(
        config, model=req.model, edit_mode=edit_mode, tier=tier, route=primary
    )

    quality = req.quality or config.get("default_quality", "xhigh")
    if quality not in VALID_QUALITIES:
        raise PdogError(
            f"Invalid quality '{quality}'. Valid values: {', '.join(VALID_QUALITIES)}.",
            kind="invalid_parameter",
        )

    # The 4K route's async validation only accepts low/medium/high (实测
    # 2026-09-16: quality=xhigh fails with invalid_request_error before any
    # render), so downgrade richer tiers on 4K and note it in the result.
    quality_note: Optional[str] = None
    if tier == "4k" and quality in ("xhigh", "max"):
        quality_note = (
            f"quality {quality} -> high：4K 网关异步校验仅接受 low/medium/high"
        )
        quality = "high"

    payload: dict[str, Any] = {
        "model": model_id,
        "prompt": req.prompt.strip(),
        "output_format": "png",
        "response_format": "b64_json",
        "size": final_size,
    }
    if quality:
        payload["quality"] = quality
    if req.n > 1:
        payload["n"] = req.n

    transparent_requested = req.background == "transparent"
    chroma_mode = transparent_requested and req.transparent_strategy == "chroma-key"
    key_color: Optional[Tuple[int, int, int]] = None
    key_details: Optional[dict] = None
    if chroma_mode:
        if not images:
            raise PdogError(
                "Dynamic chroma-key transparency requires at least one reference image.",
                kind="invalid_parameter",
            )
        key_color, key_details = _resolve_chroma_key(req, images[0][2])
        key_hex = "#%02X%02X%02X" % key_color
        payload["prompt"] = (
            payload["prompt"]
            + f"\n\n透明背景执行要求：不要直接生成透明通道。请把所有需要透明的最底层背景替换成"
              f"完全均匀、单一、无纹理的纯色 {key_hex}（RGB {key_color[0]}, {key_color[1]}, {key_color[2]}）。"
              "该纯色只能出现在待删除背景，不得用于主体、图标、边缘、反光或阴影；不要绘制棋盘格、渐变、噪点、压缩纹理或背景阴影。"
              "主体与纯色背景之间保持清晰、抗锯齿的边界。"
        )
    elif req.background:
        payload["background"] = req.background

    async_cfg = config.get("async", {}) if isinstance(config.get("async"), dict) else {}
    async_mode = req.async_mode or async_cfg.get("mode", "auto")
    if async_mode not in VALID_ASYNC_MODES:
        async_mode = "auto"
    poll_interval = float(
        req.poll_interval if req.poll_interval is not None
        else async_cfg.get("poll_interval_seconds", 5)
    )
    task_timeout = float(
        req.task_timeout if req.task_timeout is not None
        else async_cfg.get("task_timeout_seconds", 1800)
    )
    if poll_interval <= 0 or task_timeout <= 0:
        raise PdogError(
            "poll_interval and task_timeout must be greater than zero.", kind="invalid_parameter"
        )

    # 4K jobs routinely exceed the sync wait, so auto mode prefers the async task API.
    use_async = async_mode == "always" or (async_mode == "auto" and tier == "4k")

    if req.dry_run:
        preview = {
            "endpoint": get_async_endpoint(base_url, edit_mode=edit_mode) if use_async
            else get_endpoint(base_url, edit_mode=edit_mode),
            "transport": "async" if use_async else "sync",
            "async_mode": async_mode,
            "model": model_id,
            "model_reason": model_reason,
            "tier": tier,
            "api_key": mask_key(api_key),
            "route": primary.name,
            "fallback_routes": [
                {"route": route.name, "base_url": route.base_url}
                for route in routes[1:]
            ],
            "mode": "edit" if edit_mode else "text-to-image",
            "content_type": "multipart/form-data" if edit_mode else "application/json",
            "payload": payload,
            "images": [f"<file={name}, mime={mime}, bytes={len(content)}>" for name, mime, content in images],
            "mask": f"<file={mask[0]}, mime={mask[1]}, bytes={len(mask[2])}>" if mask else None,
            "transparent_strategy": "chroma-key" if chroma_mode else req.transparent_strategy,
            "key_color": "#%02X%02X%02X" % key_color if key_color else None,
            "key_selection": key_details,
        }
        if tier == "4k" and not req.allow_4k:
            preview["allow_4k_required"] = True
        return {"ok": True, "dry_run": True, "preview": preview}

    timeout_sec = float(req.timeout if req.timeout is not None else config.get("timeout_seconds", 300))
    transport_note = "sync"

    legacy_model = (config.get("models", {}) or {}).get("image-2") or "gpt-image-2"
    model_fallback_note: Optional[str] = None
    route_fallback_note: Optional[str] = None

    def _model_not_found(exc: PdogError) -> bool:
        text = f"{exc.message} {exc.hint or ''}".lower()
        return (
            "model_not_found" in text
            or "not supported by any configured account" in text
        )

    def _should_fallback_to_legacy(exc: PdogError) -> bool:
        """4K 账户组当前可能只配置了旧版 gpt-image-2（实测 flare/sunburst 全路由 404）。
        此时自动回落 image-2 一次，并在结果中如实标注模型切换。"""
        if tier != "4k" or model_id == legacy_model or req.dry_run:
            return False
        return exc.http_status == 404 or _model_not_found(exc)

    def _sync_extract(api_resp: bytes, api_key_used: str, base_url_used: str) -> List[bytes]:
        try:
            return extract_images(api_resp, api_key=api_key_used, base_url=base_url_used)
        except PdogError as exc:
            response_cache = Path.cwd() / "output" / (
                f"pdog-response-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            )
            try:
                response_cache.parent.mkdir(parents=True, exist_ok=True)
                response_cache.write_bytes(api_resp)
                exc.hint = f"{exc.hint or ''} 上游原始响应已保存到 {response_cache}，可据此恢复下载。".strip()
            except OSError:
                pass
            raise

    def _switch_to_legacy() -> None:
        nonlocal model_id, model_fallback_note, payload
        fallback_note = (
            f"{model_id} -> {legacy_model}: 网关 4K 账户组当前不提供 2.5 模型族，"
            "已自动改用 gpt-image-2"
        )
        payload = {**payload, "model": legacy_model}
        model_id, model_fallback_note = legacy_model, fallback_note
        log(fallback_note)

    def _model_for_route(route: Route) -> Tuple[str, str]:
        """Model ID to use on `route`, plus a note when it differs from the primary."""
        try:
            candidate, reason = resolve_model(
                config, model=req.model, edit_mode=edit_mode, tier=tier, route=route
            )
            return candidate, reason
        except PdogError:
            # An explicit model that this route does not serve: use the route's
            # own auto model rather than failing the whole fallback.
            if req.model == "auto":
                raise
            candidate = route.model_for(tier, edit_mode)
            if not candidate:
                raise
            return candidate, f"fallback-auto({route.name})"

    def _run_on_route(route: Route) -> Tuple[List[bytes], str, str, str, str]:
        """Execute on one route.

        Returns (images, transport, model_used, model_reason_used, route_name).
        Raises PdogError; `failover_safe` on the error says whether the next
        route may be attempted.
        """
        route_key = route.key_for(tier)
        route_model, route_reason = _model_for_route(route)
        route_payload = {**payload, "model": route_model}

        if use_async:
            task_id = submit_async_task(
                route.base_url,
                route_key,
                route_payload,
                images=images,
                mask=mask,
                timeout_seconds=timeout_sec,
                max_retries=req.retries,
            )
            if on_event:
                on_event({
                    "type": "submitted",
                    "task_id": task_id,
                    "model": route_model,
                    "route": route.name,
                })
            # Once a task id exists the job is billed: poll failures must not
            # trigger a resubmission on another route.
            try:
                polled = poll_async_task(
                    route.base_url,
                    route_key,
                    task_id,
                    poll_interval=poll_interval,
                    task_timeout=task_timeout,
                    block_seconds=req.block_seconds,
                    on_event=on_event,
                )
            except PdogError as exc:
                exc.failover_safe = False
                exc.route = route.name
                raise
            if polled["state"] == "pending":
                return _JobOutcome(
                    images=[],
                    transport=f"async({task_id})",
                    model=route_model,
                    model_reason=route_reason,
                    route=route.name,
                    pending_task_id=task_id,
                    pending_status=polled["status"],
                )
            return _JobOutcome(
                images=polled["images"],
                transport=f"async({task_id})",
                model=route_model,
                model_reason=route_reason,
                route=route.name,
            )

        api_resp = request_image(
            route.base_url,
            route_key,
            route_payload,
            images=images,
            mask=mask,
            timeout_seconds=max(timeout_sec, 600.0) if tier == "4k" else timeout_sec,
            max_retries=req.retries,
        )
        return _JobOutcome(
            images=_sync_extract(api_resp, route_key, route.base_url),
            transport="sync",
            model=route_model,
            model_reason=route_reason,
            route=route.name,
        )

    images_data: List[bytes] = []
    last_error: Optional[PdogError] = None
    tried: List[str] = []
    outcome: Optional[_JobOutcome] = None
    for index, route in enumerate(routes):
        tried.append(route.name)
        try:
            outcome = _run_on_route(route)
        except PdogError as exc:
            exc.route = exc.route or route.name
            last_error = exc

            # Same-route 4K legacy model fallback keeps its original behaviour:
            # a model the 4K group does not serve is retried once on the same
            # route with the legacy model, over the sync endpoint.
            if _should_fallback_to_legacy(exc):
                _switch_to_legacy()
                try:
                    api_resp = request_image(
                        route.base_url, route.key_for(tier), payload,
                        images=images, mask=mask,
                        timeout_seconds=max(timeout_sec, 600.0) if tier == "4k" else timeout_sec,
                        max_retries=req.retries,
                    )
                    outcome = _JobOutcome(
                        images=_sync_extract(api_resp, route.key_for(tier), route.base_url),
                        transport="sync",
                        model=legacy_model,
                        model_reason=model_reason,
                        route=route.name,
                    )
                    break
                except PdogError as retry_exc:
                    retry_exc.route = retry_exc.route or route.name
                    last_error = retry_exc
                    exc = retry_exc

            if not exc.failover_safe or index == len(routes) - 1:
                raise
            next_route = routes[index + 1]
            note = (
                f"route {route.name} -> {next_route.name}: "
                f"{exc.message}；已切换备用路由"
            )
            log(note)
            route_fallback_note = (
                f"{note}（已尝试：{', '.join(tried)}）"
                if not route_fallback_note
                else f"{route_fallback_note}；{note}"
            )
            if on_event:
                on_event({
                    "type": "route_fallback",
                    "from": route.name,
                    "to": next_route.name,
                    "reason": exc.message,
                })
            continue

        # A successful route may differ from the primary; record the switch with
        # the model it actually used.
        if outcome.route != primary.name and outcome.model != model_id:
            model_fallback_note = (
                f"{model_id} -> {outcome.model}: 备用路由 {outcome.route} 使用自己的模型"
            )
        model_id, model_reason = outcome.model, outcome.model_reason
        break
    else:  # pragma: no cover - loop exits via break or raise
        if last_error is not None:
            raise last_error
        raise PdogError("No route could serve the request.", kind="config")

    if outcome is None:  # pragma: no cover - defensive
        raise PdogError("No route produced a result.", kind="config")

    applied_route = outcome.route
    images_data = outcome.images
    transport_note = outcome.transport

    if outcome.pending_task_id:
        return {
            "ok": True,
            "pending": True,
            "task_id": outcome.pending_task_id,
            "task_status": outcome.pending_status,
            "mode": "edit" if edit_mode else "text-to-image",
            "model": model_id,
            "model_reason": model_reason,
            "tier": tier,
            "requested_size": final_size,
            "transport": transport_note,
            "route": applied_route,
            "route_fallback": route_fallback_note,
            "note": "任务已提交并计费，切勿重复提交；稍后用 pdog_task_status(task_id) 继续查询。",
        }

    first_output = (
        Path(req.output).expanduser() if req.output else default_output()
    )
    finalized = _finalize_images(
        images_data,
        req,
        first_output=first_output,
        key_color=key_color,
        transport_note=transport_note,
        on_event=on_event,
    )

    result: dict[str, Any] = {
        "ok": True,
        "mode": "edit" if edit_mode else "text-to-image",
        "model": model_id,
        "model_reason": model_reason,
        "model_fallback": model_fallback_note,
        "quality_note": quality_note,
        "tier": tier,
        "requested_size": final_size,
        "transport": transport_note,
        "route": applied_route,
        "route_fallback": route_fallback_note,
        **finalized,
    }
    actual = [(f["width"], f["height"]) for f in finalized["files"] if f.get("role") == "final" and f.get("width")]
    if actual:
        result["actual_size"] = f"{actual[0][0]}x{actual[0][1]}"
    return result


def collect_async_result(
    config: dict[str, Any],
    task_id: str,
    req: Optional[GenRequest] = None,
    *,
    wait_seconds: float = 0.0,
    on_event: Optional[OnEvent] = None,
) -> dict[str, Any]:
    """Continue polling a previously submitted async task and save its result."""
    req = req or GenRequest(prompt="(async task continuation)")
    async_cfg = config.get("async", {}) if isinstance(config.get("async"), dict) else {}
    poll_interval = float(
        req.poll_interval if req.poll_interval is not None
        else async_cfg.get("poll_interval_seconds", 5)
    )
    task_timeout = float(
        req.task_timeout if req.task_timeout is not None
        else async_cfg.get("task_timeout_seconds", 1800)
    )
    polled = poll_async_task(
        config["base_url"],
        _key_for_task(config, req, task_id),
        task_id,
        poll_interval=poll_interval,
        task_timeout=task_timeout,
        block_seconds=wait_seconds if wait_seconds > 0 else None,
        on_event=on_event,
    )
    if polled["state"] == "pending":
        return {
            "ok": True,
            "pending": True,
            "task_id": task_id,
            "task_status": polled["status"],
            "note": "任务仍在运行，已计费，切勿重复提交；稍后再用 pdog_task_status 查询。",
        }

    first_output = Path(req.output).expanduser() if req.output else default_output()
    finalized = _finalize_images(
        polled["images"],
        req,
        first_output=first_output,
        key_color=None,
        transport_note=f"async({task_id})",
        on_event=on_event,
    )
    result: dict[str, Any] = {
        "ok": True,
        "mode": "edit" if req.images else "text-to-image",
        "task_id": task_id,
        "tier": "4k" if req.is_4k else (req.resolution or ""),
        "requested_size": req.size or "",
        "transport": f"async({task_id})",
        **finalized,
    }
    actual = [(f["width"], f["height"]) for f in finalized["files"] if f.get("role") == "final" and f.get("width")]
    if actual:
        result["actual_size"] = f"{actual[0][0]}x{actual[0][1]}"
    return result


def _key_for_task(config: dict[str, Any], req: GenRequest, task_id: str) -> str:
    """Async polling must use the same key that submitted the task."""
    tier, key, _size = resolve_tier(
        config, is_4k=req.is_4k, resolution=req.resolution, size=req.size
    )
    return key


def chroma_key_file(
    image_bytes: bytes,
    *,
    key_color: str = "auto",
    tolerance: float = 30.0,
    feather: float = 40.0,
    despill_distance: float = 55.0,
    despill_radius: int = 6,
) -> Tuple[bytes, dict[str, Any]]:
    """Standalone chroma-key extraction on arbitrary image bytes (no API cost)."""
    from . import chroma as _chroma

    _chroma._require_available()
    if tolerance < 0 or feather <= 0:
        raise PdogError(
            "Chroma tolerance must be non-negative and feather must be positive.",
            kind="invalid_parameter",
        )
    if despill_distance < 0 or despill_radius < 0:
        raise PdogError(
            "Chroma despill distance and radius must be non-negative.", kind="invalid_parameter"
        )
    if key_color == "auto":
        color, selection = _chroma.choose_key_color(image_bytes)
    else:
        try:
            color = _chroma.parse_hex_color(key_color)
        except ValueError as exc:
            raise PdogError(str(exc), kind="invalid_parameter") from exc
        selection = {"selected": _chroma.color_hex(color), "scores": None}
    try:
        output, metrics = _chroma.extract_edge_connected_key(
            image_bytes,
            color,
            tolerance=tolerance,
            feather=feather,
            despill_distance=despill_distance,
            despill_radius=despill_radius,
        )
    except ValueError as exc:
        raise PdogError(
            f"Chroma-key extraction failed: {exc}",
            kind="upstream",
            hint="调整 tolerance/feather 或换 key_color 后重试；本工具不调用 API，不产生费用。",
        ) from exc
    metrics["key_selection"] = selection
    return output, metrics
