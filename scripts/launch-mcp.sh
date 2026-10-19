#!/usr/bin/env bash
# Launcher for the pdog-image MCP server.
#
# GUI-spawned processes (ZCode launched from Dock/Finder) do not inherit the
# shell's PATH, so `uv` may be missing even though it is installed. This
# launcher probes the standard install locations itself, keeping the server
# start independent of the spawning process's environment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

UV=""
if command -v uv >/dev/null 2>&1; then
  UV="$(command -v uv)"
else
  for candidate in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$candidate" ]; then
      UV="$candidate"
      break
    fi
  done
fi

if [ -z "$UV" ]; then
  echo "[pdog-image] uv not found (looked in PATH, ~/.local/bin, /opt/homebrew/bin, /usr/local/bin)." >&2
  echo "[pdog-image] Install uv first: https://docs.astral.sh/uv/" >&2
  exit 127
fi

# --extra chroma installs the color-key engine on first launch. Native
# transparency is rejected by this gateway, so chroma-key is the only route to
# transparent output; without the extra that tool fails at runtime.
exec "$UV" run --project "$ROOT" --extra chroma pdog-image-mcp
