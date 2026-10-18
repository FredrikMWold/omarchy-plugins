#!/bin/bash
set -euo pipefail
plugin_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_path="${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.dreame/venv/bin/python"
if [[ ! -x "$python_path" ]]; then
  printf '%s\n' '{"ok":false,"error":{"code":"SETUP_REQUIRED","message":"Run Dreame setup to install its private Python environment."}}'
  exit 1
fi
exec "$python_path" "$plugin_dir/backend.py" "$@"
