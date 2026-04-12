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

mkdir -p \
  "${HOME}/.gemini" \
  "${HOME}/.codex" \
  "${HOME}/.config/github-copilot"

link_skill_root "${HOME}/.codex/skills"
link_skill_root "${HOME}/.gemini/skills"
link_skill_root "${HOME}/.config/github-copilot/skills"

exec "$@"
