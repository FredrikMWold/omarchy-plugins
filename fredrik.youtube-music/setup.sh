#!/bin/bash

set -euo pipefail

plugin_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

omarchy pkg add nodejs npm python-ytmusicapi
omarchy pkg aur add python-browser-cookie3
npm install --omit=dev --prefix "$plugin_dir"
exec python "$plugin_dir/ytmusic_bridge.py" setup
