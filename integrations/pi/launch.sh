#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export PERSONAL_VAULT_ROOT="${PERSONAL_VAULT_ROOT:-$HOME/Documents/AI对话归档/personal-memory-vault}"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"
export PERSONAL_VAULT_SEMANTIC_PYTHON="${PERSONAL_VAULT_SEMANTIC_PYTHON:-$repo_root/.venv/bin/python}"
mkdir -p "$PERSONAL_VAULT_ROOT/runtime/pi/sessions"
cd "$PERSONAL_VAULT_ROOT/runtime/pi"
exec pi --no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files \
  --no-approve --no-builtin-tools --extension "$repo_root/integrations/pi/memory.ts" \
  --system-prompt "$repo_root/integrations/pi/system.md" \
  --session-dir "$PERSONAL_VAULT_ROOT/runtime/pi/sessions" "$@"
