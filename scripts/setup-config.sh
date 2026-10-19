#!/usr/bin/env bash
# Write a first-run Pdog config to ~/.pdog-image/config.json.
#
# Usage:
#   ./scripts/setup-config.sh                       # interactive
#   ./scripts/setup-config.sh --base-url URL --key sk-xxx [--key-4k sk-yyy]
#   ./scripts/setup-config.sh --force               # overwrite an existing file
#
# The key never appears in shell history: without --key/--key-4k the script
# prompts for it with hidden input.
set -euo pipefail

BASE_URL=""
KEY_1K=""
KEY_4K=""
MODEL_GEN="gpt-image-2.5-flare"
MODEL_EDIT="gpt-image-2.5-sunburst"
MODEL_4K="gpt-image-2"
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --base-url) BASE_URL="${2:-}"; shift 2 ;;
    --key) KEY_1K="${2:-}"; shift 2 ;;
    --key-4k) KEY_4K="${2:-}"; shift 2 ;;
    --model) MODEL_GEN="${2:-}"; shift 2 ;;
    --model-edit) MODEL_EDIT="${2:-}"; shift 2 ;;
    --model-4k) MODEL_4K="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

CONFIG="$HOME/.pdog-image/config.json"
if [ -f "$CONFIG" ] && [ "$FORCE" -ne 1 ]; then
  echo "Config already exists: $CONFIG"
  echo "Re-run with --force to overwrite (a copy is kept as config.json.bak)."
  exit 1
fi

if [ -z "$BASE_URL" ]; then
  printf "Gateway base URL [https://ai.whyshy.cn]: "
  read -r BASE_URL
  BASE_URL="${BASE_URL:-https://ai.whyshy.cn}"
fi

if [ -z "$KEY_1K" ]; then
  printf "API key for 1K/2K (hidden): "
  read -rs KEY_1K; echo
fi
if [ -z "$KEY_4K" ]; then
  printf "API key for 4K [same as above]: "
  read -rs KEY_4K; echo
  KEY_4K="${KEY_4K:-$KEY_1K}"
fi

if [ -z "$KEY_1K" ]; then
  echo "An API key is required." >&2
  exit 2
fi

mkdir -p "$HOME/.pdog-image"
chmod 700 "$HOME/.pdog-image"
if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.bak"
  chmod 600 "$CONFIG.bak"
fi

PDOG_BASE_URL="$BASE_URL" PDOG_KEY_1K="$KEY_1K" PDOG_KEY_4K="$KEY_4K" \
PDOG_MODEL_GEN="$MODEL_GEN" PDOG_MODEL_EDIT="$MODEL_EDIT" PDOG_MODEL_4K="$MODEL_4K" \
python3 - "$CONFIG" <<'PY'
import json, os, sys

path = sys.argv[1]
cfg = {
    "_comment": "Multi-route config: routes are tried in order; unbilled failures fall through to the next route. 1k/2k share api_keys.1k, 4k uses api_keys.4k.",
    "routes": [{
        "name": "main",
        "base_url": os.environ["PDOG_BASE_URL"],
        "api_keys": {
            "1k": os.environ["PDOG_KEY_1K"],
            "4k": os.environ["PDOG_KEY_4K"],
        },
        "generation": os.environ["PDOG_MODEL_GEN"],
        "edit": os.environ["PDOG_MODEL_EDIT"],
        "generation_4k": os.environ["PDOG_MODEL_4K"],
        "edit_4k": os.environ["PDOG_MODEL_4K"],
    }],
    "default_resolution": "1k",
    "default_quality": "xhigh",
    "timeout_seconds": 300,
    "async": {"mode": "auto", "poll_interval_seconds": 5, "task_timeout_seconds": 1800},
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
os.chmod(path, 0o600)
PY

echo "Wrote $CONFIG (mode 600)."
echo "Add a backup route later by editing the routes array — order defines the fallback."
