#!/bin/bash
set -euo pipefail
umask 077
plugin_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.dreame"
if [[ -L "$data_dir" || ( -e "$data_dir" && ! -O "$data_dir" ) ]]; then
  echo "Dreame storage must be owned by you and must not be a symlink." >&2
  exit 1
fi
mkdir -p -m 700 "$data_dir"
chmod 700 "$data_dir"
export UV_PROJECT_ENVIRONMENT="$data_dir/venv"
if [[ -x /usr/bin/uv ]]; then
  uv_command=(/usr/bin/uv)
elif command -v uv >/dev/null && uv --version >/dev/null 2>&1; then
  uv_command=(uv)
elif command -v mise >/dev/null && mise exec uv@0.12.15 -- uv --version >/dev/null 2>&1; then
  uv_command=(mise exec uv@0.12.15 -- uv)
else
  echo "A usable uv is required. Install it with: omarchy pkg add uv" >&2
  exit 1
fi
"${uv_command[@]}" sync --project "$plugin_dir" --locked --no-dev --python /usr/bin/python
"$UV_PROJECT_ENVIRONMENT/bin/python" "$plugin_dir/backend.py" initialize
printf '\nDreame dependencies and app profile are ready. No account login or device command was sent.\n'
printf 'Choose Sign in in the plugin, or run: bash "%s/login.sh"\n' "$plugin_dir"
if [[ -t 0 && -t 1 ]]; then
  read -r -p "Press Enter to close this terminal." _
fi
