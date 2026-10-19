#!/usr/bin/env bash
# Publish the workspace plugin into the ZCode local-marketplace snapshot.
#
# The Discover tab reads the SNAPSHOT under
#   ~/.zcode/cli/plugins/marketplaces/hx-zcode-work/
# (refreshed when the marketplace is added/refreshed), not the live
# workspace. After editing the plugin here, run this script, then update the
# plugin in Settings → Plugin Management → Discover, and start a new session.
#
# Before publishing, bump the version in BOTH:
#   .zcode-plugin/plugin.json  and  ../marketplace.json
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"          # .../zw-zcode-plugins/pdog-image2-mcp
MARKET_ROOT="$(dirname "$SRC")"                   # .../zw-zcode-plugins
CACHE="$HOME/.zcode/cli/plugins/marketplaces/hx-zcode-work"

VERSION=$(python3 -c "import json;print(json.load(open('$SRC/.zcode-plugin/plugin.json'))['version'])")
# Target dir name comes from the marketplace manifest's source path
# (e.g. ./pdog-image2-mcp), NOT from the plugin name — they differ.
TARGET=$(python3 -c "import json;print(json.load(open('$MARKET_ROOT/marketplace.json'))['plugins'][0]['source'].lstrip('./'))")

rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  --exclude 'output' --exclude '.DS_Store' \
  "$SRC/" "$CACHE/$TARGET/"
cp "$MARKET_ROOT/marketplace.json" "$CACHE/marketplace.json"

echo "published $TARGET (v$VERSION) -> marketplace snapshot"
echo "next: Settings → Plugin Management → Discover → update the plugin, then start a new session."
