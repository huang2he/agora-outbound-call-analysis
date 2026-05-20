#!/usr/bin/env bash
# 调用 filter.py 的 wrapper, 用 skill 的 .venv. 与 dashboard 主流程独立.
#
# 用法 (放进 ~/bin 加 PATH, 或直接走绝对路径):
#   bash scripts/tools/filter.sh INPUT.csv [--bjt "5-21 10:00 - 12:00"] [--agent lxc] ...
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$HERE/../.." && pwd)"
VENV="$SKILL_DIR/.venv"
if [ ! -d "$VENV" ]; then
  echo "[setup] .venv 不存在, 先跑一次 scripts/run.sh 完成初始化" >&2
  exit 1
fi
exec "$VENV/bin/python" "$HERE/filter.py" "$@"
