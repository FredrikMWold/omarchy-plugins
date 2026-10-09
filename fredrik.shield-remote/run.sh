#!/bin/bash
set -euo pipefail
plugin_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_path="${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.shield-remote/venv/bin/python"
if [[ ! -x "$python_path" ]]; then
  echo "Shield Remote dependencies are missing. Run the plugin's setup.sh." >&2
  exit 1
fi
exec "$python_path" "$plugin_dir/router.py" "$@"
