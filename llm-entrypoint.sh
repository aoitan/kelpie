#!/bin/sh
set -eu

KELPIE_SKILLS_DIR="${KELPIE_TEMPLATE_DIR:-/opt/kelpie}/skills"

link_skill_root() {
  target=$1

  if [ ! -d "$KELPIE_SKILLS_DIR" ]; then
    return 0
  fi

  parent=$(dirname "$target")
  mkdir -p "$parent"

  if [ -L "$target" ]; then
    ln -sfn "$KELPIE_SKILLS_DIR" "$target"
    return 0
  fi

  if [ ! -e "$target" ]; then
    ln -s "$KELPIE_SKILLS_DIR" "$target"
    return 0
  fi

  if [ ! -d "$target" ]; then
    echo "kelpie: skip skill link for non-directory target: $target" >&2
    return 0
  fi

  for entry in "$KELPIE_SKILLS_DIR"/*; do
    [ -e "$entry" ] || continue
    name=$(basename "$entry")
    dest="$target/$name"
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
      continue
    fi
    ln -sfn "$entry" "$dest"
  done
}

# Some tools (e.g. Node.js os.userInfo()) require the running UID to have a
# /etc/passwd entry. When running as an arbitrary UID (common in Docker with
# `user: "$UID:$GID"`), use libnss-wrapper to inject a synthetic entry.
if ! getent passwd "$(id -u)" > /dev/null 2>&1; then
  _nss_wrapper=$(find /usr/lib -name 'libnss_wrapper.so' 2>/dev/null | head -1)
  if [ -n "$_nss_wrapper" ]; then
    _nss_passwd=$(mktemp)
    _nss_group=$(mktemp)
    cat /etc/passwd > "$_nss_passwd"
    echo "dev:x:$(id -u):$(id -g):dev:${HOME}:/bin/sh" >> "$_nss_passwd"
    cat /etc/group > "$_nss_group"
    export NSS_WRAPPER_PASSWD="$_nss_passwd"
    export NSS_WRAPPER_GROUP="$_nss_group"
    export LD_PRELOAD="$_nss_wrapper"
  fi
fi

mkdir -p \
  "${HOME}/.gemini" \
  "${HOME}/.codex" \
  "${HOME}/.config/github-copilot"

link_skill_root "${HOME}/.codex/skills"
link_skill_root "${HOME}/.gemini/skills"
link_skill_root "${HOME}/.config/github-copilot/skills"

exec "$@"
