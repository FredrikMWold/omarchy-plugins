#!/bin/bash

set -euo pipefail

uid=$(id -u)
if [[ ! "$uid" =~ ^[0-9]+$ ]]; then
  echo "Could not determine the current user ID" >&2
  exit 1
fi

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$uid}
if [[ ! -d "$runtime_dir" || ! -O "$runtime_dir" ]]; then
  echo "A secure user runtime directory is required" >&2
  exit 1
fi

state_home=${XDG_STATE_HOME:-$HOME/.local/state}
state_dir=$state_home/fredrik.ryde-nearby
mkdir -p "$state_dir"
result=$state_dir/setup-result.json
config=
setup_complete=false

write_result() {
  local status=$1
  local exit_code=$2
  local temporary
  temporary=$(mktemp "$state_dir/setup-result.XXXXXX")
  printf '{"status":"%s","exitCode":%d,"updatedAt":%d}\n' \
    "$status" "$exit_code" "$(date +%s)" >"$temporary"
  mv "$temporary" "$result"
}

finish_setup() {
  local exit_code=$?
  if [[ -n "$config" ]]; then
    rm -f -- "$config"
  fi
  if ! "$setup_complete"; then
    write_result failure "$exit_code"
  fi
}

trap finish_setup EXIT
write_result running 0

omarchy pkg add geoclue libnotify python-gobject qt6-location

umask 077
config=$(mktemp "$runtime_dir/ryde-geoclue.XXXXXX")
cat >"$config" <<EOF
[fredrik.ryde-nearby]
allowed=true
system=false
users=$uid
EOF

sudo install -o root -g root -m 0644 "$config" \
  /etc/geoclue/conf.d/50-omarchy-ryde-nearby.conf
sudo systemctl try-restart geoclue.service
write_result success 0
setup_complete=true
omarchy restart shell
