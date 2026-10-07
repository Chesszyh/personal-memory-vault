#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
vault_root="${PERSONAL_VAULT_ROOT:-$HOME/Documents/AI对话归档/personal-memory-vault}"
for component in reader review; do
  unit="personal-vault-$component"
  if ! systemctl --user is-active --quiet "$unit.service"; then
    if systemctl --user cat "$unit.service" >/dev/null 2>&1; then
      systemctl --user start "$unit.service"
      continue
    fi
    if [[ "$component" == reader ]]; then command=read; port=8767; else command=memory; port=8766; fi
    systemd-run --user --collect --unit="$unit" --property="WorkingDirectory=$repo_root" \
      --setenv="PYTHONPATH=$repo_root/src" /usr/bin/python3 -m personal_vault "$command" serve "$vault_root" --port "$port"
  fi
done
printf '阅读工作台：http://127.0.0.1:8767/\n记忆审核：http://127.0.0.1:8766/\n'
