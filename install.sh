#!/usr/bin/env bash
# One-line installer for the Agora outbound-call analysis skill.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/huang2he/agora-outbound-call-analysis/main/install.sh | bash
#
# What it does:
#   1. Clones (or updates) the repo into ~/.claude/skills/agora-outbound-call-analysis
#   2. Runs first-time setup (creates .venv, pip installs pandas + openpyxl)
#   3. Prints next-step usage hint
set -euo pipefail

REPO="https://github.com/huang2he/agora-outbound-call-analysis.git"
TARGET="${HOME}/.claude/skills/agora-outbound-call-analysis"

say() { printf "\033[1;34m▸\033[0m %s\n" "$1" >&2; }
warn() { printf "\033[1;33m!\033[0m %s\n" "$1" >&2; }
die() { printf "\033[1;31m✗\033[0m %s\n" "$1" >&2; exit 1; }

# Sanity checks
command -v git >/dev/null 2>&1 || die "git not found. Install Xcode CLT (\`xcode-select --install\`) or your distro's git package."

PY=""
for c in python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "python3 not found on PATH. Install Python 3.10+ first."

# Verify Python version is recent enough
$PY -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" || \
  die "Python 3.10+ required (found $($PY -V 2>&1))"

# Install / update
mkdir -p "$(dirname "$TARGET")"
if [ -d "$TARGET/.git" ]; then
  say "Updating existing install at $TARGET"
  git -C "$TARGET" pull --ff-only --quiet
elif [ -d "$TARGET" ]; then
  die "$TARGET exists but isn't a git checkout. Remove it first."
else
  say "Cloning into $TARGET"
  git clone --quiet --depth 1 "$REPO" "$TARGET"
fi

# First-time venv + deps. run.sh does this itself on first invocation, but doing it
# now means the first user-facing run is instant.
if [ ! -d "$TARGET/.venv" ]; then
  say "Creating .venv (one-time, ~10s)"
  "$PY" -m venv "$TARGET/.venv"
  "$TARGET/.venv/bin/pip" install -q --upgrade pip
  "$TARGET/.venv/bin/pip" install -q pandas openpyxl
fi

printf '\n\033[1;32m✓\033[0m Installed to %s\n\n' "$TARGET"
cat <<EOF
Next step — open the dashboard with any Agora ConvoAI summary CSV:

  bash $TARGET/scripts/run.sh path/to/summary.csv

Or just tell Claude Code "分析这批外呼" + attach the CSV — the skill auto-triggers.

Update later: re-run this same install command (it git pulls in place).
EOF
