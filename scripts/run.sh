#!/usr/bin/env bash
# One-shot launcher for the Agora outbound-call dashboard.
#   - Creates a local venv on first run (only Python stdlib + pandas + openpyxl needed)
#   - Picks a free port automatically
#   - Opens the browser at the served URL
#
# Usage:
#   bash run.sh <input.csv-or-xlsx> [--port N] [--no-open]
#   bash run.sh --build <input.csv-or-xlsx> [-o out.html]   # static HTML, no server
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$HERE/.." && pwd)"
VENV="$SKILL_DIR/.venv"

PY=""
for candidate in python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "error: python3 not found on PATH" >&2
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "[setup] creating venv at $VENV (one-time)" >&2
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q pandas openpyxl
fi

# Vendor JS libs (echarts + xlsx). Inlining them into the generated HTML means
# the dashboard works offline and survives CDN flakiness / GFW blocks.
VENDOR="$SKILL_DIR/vendor"
if [ ! -f "$VENDOR/echarts.min.js" ] || [ ! -f "$VENDOR/xlsx.full.min.js" ]; then
  echo "[setup] caching ECharts + SheetJS to $VENDOR (one-time, ~2MB)" >&2
  mkdir -p "$VENDOR"
  curl -fsSL -o "$VENDOR/echarts.min.js"   https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js \
    || echo "  WARN: echarts download failed; HTML will fall back to CDN tags" >&2
  curl -fsSL -o "$VENDOR/xlsx.full.min.js" https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js \
    || echo "  WARN: xlsx download failed; HTML will fall back to CDN tags" >&2
fi

if [ "${1:-}" = "--build" ]; then
  shift
  exec "$VENV/bin/python" "$HERE/build_dashboard.py" "$@"
else
  exec "$VENV/bin/python" "$HERE/serve_dashboard.py" "$@"
fi
