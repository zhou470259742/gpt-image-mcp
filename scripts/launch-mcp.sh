#!/usr/bin/env bash
# Launcher for the pdog-image MCP server.
#
# GUI-spawned processes (ZCode launched from Dock/Finder) do not inherit the
# shell's PATH, so `uv` may be missing even though it is installed. This
# launcher probes the standard install locations itself.
#
# If uv is genuinely absent it installs it once via the official installer.
# Auto-install runs only when the server is started by the plugin itself; set
# PDOG_NO_AUTO_UV=1 to opt out (the launcher then fails with instructions).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
log() { echo "[pdog-image] $*" >&2; }

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  for candidate in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.cargo/bin/uv"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

download() {
  # Prefer curl; fall back to wget. Honors https_proxy / ZCODE_HTTP_PROXY.
  if [ -n "${ZCODE_HTTP_PROXY:-}" ]; then
    export https_proxy="$ZCODE_HTTP_PROXY" http_proxy="$ZCODE_HTTP_PROXY"
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf --max-time 180 "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --timeout=180 -O "$2" "$1"
  else
    log "Neither curl nor wget is available; cannot download uv."
    return 1
  fi
}

install_uv() {
  local script
  script="$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX.sh")"

  log "uv not found; installing it once via https://astral.sh/uv/install.sh ..."
  if ! download "https://astral.sh/uv/install.sh" "$script"; then
    rm -f "$script"
    log "Failed to download the uv installer."
    return 1
  fi
  if ! grep -q "astral.sh\|uv" "$script" 2>/dev/null; then
    rm -f "$script"
    log "Downloaded installer does not look like the uv installer; refusing to run it."
    return 1
  fi
  # The official installer is idempotent and installs to ~/.local/bin by default.
  if ! sh "$script" >&2; then
    rm -f "$script"
    log "uv installer failed."
    return 1
  fi
  rm -f "$script"
  return 0
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
  if [ "${PDOG_NO_AUTO_UV:-0}" = "1" ]; then
    log "uv not found and PDOG_NO_AUTO_UV=1 is set."
    log "Install it manually: https://docs.astral.sh/uv/  (or unset PDOG_NO_AUTO_UV)"
    exit 127
  fi
  if ! install_uv; then
    log "Automatic uv installation failed."
    log "Install it manually: https://docs.astral.sh/uv/"
    exit 127
  fi
  # The installer adds ~/.local/bin to shell rc files, which a GUI process has
  # not sourced; probe the file directly instead of re-reading PATH.
  UV="$(find_uv || true)"
  if [ -z "$UV" ]; then
    log "uv was installed but could not be located under ~/.local/bin or PATH."
    exit 127
  fi
  log "uv installed at $UV"
fi

# --extra chroma installs the color-key engine on first launch. Native
# transparency is rejected by this gateway, so chroma-key is the only route to
# transparent output; without the extra that tool fails at runtime.
exec "$UV" run --project "$ROOT" --extra chroma pdog-image-mcp
