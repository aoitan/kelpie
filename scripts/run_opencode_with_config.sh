#!/bin/sh
set -eu

config_path=${KELPIE_OPENCODE_CONFIG:-/kelpie-config/opencode.json}
state_root=${KELPIE_OPENCODE_STATE_DIR:-/workspace/.data/opencode}

if [ ! -f "$config_path" ] || [ ! -r "$config_path" ]; then
  echo "kelpie-opencode: config file is missing or unreadable: $config_path" >&2
  exit 2
fi

mkdir -p "$state_root/data" "$state_root/cache" "$state_root/state"

export XDG_DATA_HOME="$state_root/data"
export XDG_CACHE_HOME="$state_root/cache"
export XDG_STATE_HOME="$state_root/state"
export OPENCODE_DISABLE_AUTOUPDATE=${OPENCODE_DISABLE_AUTOUPDATE:-true}
OPENCODE_CONFIG_CONTENT=$(sed -n '1,$p' "$config_path")
export OPENCODE_CONFIG_CONTENT

exec opencode "$@"
