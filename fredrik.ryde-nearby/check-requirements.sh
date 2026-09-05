#!/bin/bash

set -u

as_json_bool() {
  if "$@"; then printf true; else printf false; fi
}

qt_ready=false
python_ready=false
geoclue_ready=false
agent_ready=false
authorization_ready=false
authorization_uid_scoped=false
authorization_status=Missing

if pacman -Q qt6-location >/dev/null 2>&1 \
  && [[ -d /usr/lib/qt6/qml/QtLocation ]] \
  && [[ -d /usr/lib/qt6/qml/QtPositioning ]]; then
  qt_ready=true
fi

if command -v python >/dev/null 2>&1 \
  && python -c 'import gi' >/dev/null 2>&1; then
  python_ready=true
fi

if pacman -Q geoclue >/dev/null 2>&1 \
  && [[ -f /usr/share/dbus-1/system-services/org.freedesktop.GeoClue2.service ]] \
  && python -c 'import gi; gi.require_version("Geoclue", "2.0"); from gi.repository import Geoclue, GLib' >/dev/null 2>&1; then
  geoclue_ready=true
fi

if pacman -Q libnotify >/dev/null 2>&1 \
  && [[ -x /usr/lib/geoclue-2.0/demos/agent ]] \
  && ! ldd /usr/lib/geoclue-2.0/demos/agent 2>/dev/null \
    | grep -q 'not found'; then
  agent_ready=true
fi

config=/etc/geoclue/conf.d/50-omarchy-ryde-nearby.conf
if [[ -f "$config" ]]; then
  read_setting() {
    awk -F= -v key="$1" '
      $0 == "[fredrik.ryde-nearby]" { active = 1; next }
      /^\[/ { active = 0 }
      active && $1 == key { print substr($0, index($0, "=") + 1); exit }
    ' "$config"
  }
  section_count=$(grep -Fxc '[fredrik.ryde-nearby]' "$config")
  allowed=$(read_setting allowed)
  system=$(read_setting system)
  users=$(read_setting users)
  owner=$(stat -c %u "$config" 2>/dev/null || printf invalid)
  mode=$(stat -c %a "$config" 2>/dev/null || printf 0)
  uid=$(id -u)
  if [[ "$users" == "$uid" ]]; then
    authorization_uid_scoped=true
  fi
  if (( section_count == 1 )) \
    && [[ "$allowed" == true && "$system" == false && "$owner" == 0 ]] \
    && (( (8#$mode & 022) == 0 )); then
    if "$authorization_uid_scoped"; then
      authorization_ready=true
      authorization_status=Ready
    elif [[ -z "$users" ]]; then
      authorization_status="Needs hardening"
    fi
  fi
fi

ready=false
if "$qt_ready" && "$python_ready" && "$geoclue_ready" && "$agent_ready" \
  && "$authorization_ready"; then
  ready=true
fi

qt_status=Missing
python_status=Missing
geoclue_status=Missing
agent_status=Missing
if "$qt_ready"; then qt_status=Ready; fi
if "$python_ready"; then python_status=Ready; fi
if "$geoclue_ready"; then geoclue_status=Ready; fi
if "$agent_ready"; then agent_status=Ready; fi

cat <<JSON
{"schema":1,"ready":$ready,"uidScoped":$authorization_uid_scoped,"checks":[{"id":"qt","label":"Qt map runtime","detail":"qt6-location","ready":$qt_ready,"status":"$qt_status"},{"id":"python","label":"Python location bridge","detail":"python-gobject","ready":$python_ready,"status":"$python_status"},{"id":"geoclue","label":"GeoClue service","detail":"geoclue","ready":$geoclue_ready,"status":"$geoclue_status"},{"id":"agent","label":"GeoClue authorization agent","detail":"libnotify","ready":$agent_ready,"status":"$agent_status"},{"id":"authorization","label":"Location permission","detail":"GeoClue authorization","ready":$authorization_ready,"status":"$authorization_status"}]}
JSON
