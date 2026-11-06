#!/usr/bin/env bash
# Installs ai-job-radar into a local virtualenv, puts the `ai-job-radar` command on your PATH,
# and links the Claude Code skill into ~/.claude/skills so `/ai-job-radar` works in any session.
set -euo pipefail
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv >/dev/null 2>&1 || uv venv .venv
  uv pip install --python .venv/bin/python -e ".[dev]"
else
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -e ".[dev]"
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/.venv/bin/ai-job-radar" "$HOME/.local/bin/ai-job-radar"

if [ -d "$HOME/.claude" ] || command -v claude >/dev/null 2>&1; then
  mkdir -p "$HOME/.claude/skills"
  ln -sfn "$PWD/skills/ai-job-radar" "$HOME/.claude/skills/ai-job-radar"
  echo "Claude Code skill linked: ~/.claude/skills/ai-job-radar  (use /ai-job-radar in any session)"
fi

echo
echo "Installed. Try:"
echo "  ai-job-radar run --days 7 --country India --domain tech"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo; echo "Add ~/.local/bin to your PATH, e.g.:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
esac
