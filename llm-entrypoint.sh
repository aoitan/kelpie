#!/bin/sh
set -eu

mkdir -p \
  "${HOME}/.gemini" \
  "${HOME}/.codex" \
  "${HOME}/.config/github-copilot"

exec "$@"
