#!/bin/bash
set -euo pipefail
plugin_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for tool in omarchy omarchy-shell hyprctl bluetoothctl systemctl udevadm busctl wpctl pactl pw-record notify-send curl; do
  if ! command -v "$tool" >/dev/null; then
    echo "Missing Omarchy prerequisite: $tool" >&2
    exit 1
  fi
done
if [[ -z "${HYPRLAND_INSTANCE_SIGNATURE:-}" || -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "Run setup from a terminal in your Omarchy desktop session." >&2
  exit 1
fi
response=$(hyprctl eval 'assert(type(hl.dsp.send_shortcut) == "function" and type(hl.dsp.focus) == "function" and type(hl.dsp.exec_cmd) == "function" and type(hl.dsp.window.move) == "function" and type(hl.dsp.window.fullscreen) == "function", "Current Hyprland Lua dispatchers are required")')
if [[ "$response" != "ok" ]]; then
  printf 'Unsupported Hyprland session: %s\n' "$response" >&2
  exit 1
fi
if ! /usr/bin/python -c 'import gi; gi.require_version("Atspi", "2.0"); from gi.repository import Atspi' >/dev/null 2>&1; then
  read -r -p "Stremio focus controls need python-gobject and at-spi2-core. Install using Omarchy? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 1
  omarchy pkg add python-gobject at-spi2-core
fi

data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/fredrik.shield-remote"
if [[ -L "$data_dir" || ( -e "$data_dir" && ! -O "$data_dir" ) ]]; then
  echo "Application storage must be owned by you and machine-local: $data_dir" >&2
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
  echo "Using project-scoped mise uv; no global tool configuration is changed."
  uv_command=(mise exec uv@0.12.15 -- uv)
else
  read -r -p "No usable uv was found. Install it using Omarchy? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 1
  omarchy pkg add uv
  uv_command=(/usr/bin/uv)
fi
if ! command -v cc >/dev/null || [[ ! -r /usr/include/linux/input.h ]]; then
  read -r -p "evdev needs build tools. Install gcc and linux-api-headers using Omarchy? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 1
  omarchy pkg add gcc linux-api-headers
fi
if ! command -v cargo >/dev/null || ! command -v clang >/dev/null \
    || ! command -v pkg-config >/dev/null \
    || ! pkg-config --exists libpipewire-0.3 dbus-1; then
  read -r -p "The bundled microphone needs Rust, Clang and PipeWire/D-Bus build libraries. Install using Omarchy? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || exit 1
  omarchy pkg add rust clang pkgconf pipewire dbus
fi
"${uv_command[@]}" sync --project "$plugin_dir" --locked --no-dev --python 3.12

known_remotes=$(bluetoothctl devices)
if ! grep -q ' NVIDIA SHIELD Remote$' <<<"$known_remotes"; then
  printf '\nPut the remote in Bluetooth pairing mode (hold Home and Back until it blinks).\n'
  printf 'Find NVIDIA SHIELD Remote in the Bluetooth panel, but do NOT pair it yet.\n'
  printf 'The input safety rule must be installed before the first pairing.\n'
  omarchy-shell omarchy.bluetooth open
  read -r -p "Press Enter after the remote appears in the Bluetooth panel." _
fi
"$UV_PROJECT_ENVIRONMENT/bin/python" "$plugin_dir/router.py" initialize
printf '\nThis builds the bundled microphone, installs a rule for the selected remote ONLY,\n'
printf 'then enables the microphone and button-router user-session services.\n'
printf 'The physical remote is ignored by the desktop even while the service is stopped.\n'
read -r -p "Apply these machine-local permissions and service changes? [y/N] " answer
if [[ "$answer" != "y" && "$answer" != "Y" ]]; then
  echo "Dependencies are ready; system changes were not applied."
  exit 0
fi
systemctl --user import-environment HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY
"$UV_PROJECT_ENVIRONMENT/bin/python" "$plugin_dir/router.py" install
printf '\nInput permissions, microphone and router services are ready. If this is a new Bluetooth bond,\n'
printf 'open Shield Remote, hold Home + Back until it blinks, and select Pair remote.\n'
printf 'For an existing bond, wake it with an arrow and select Reconnect remote,\n'
printf 'and pair the Shield TV using its on-screen code.\n'
printf 'The setup button disappears once the rule, receipt and enabled service match.\n'
read -r -p "Press Enter to close this terminal." _
