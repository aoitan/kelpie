#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

KELPIE_HOME=${KELPIE_HOME:-"$HOME/.local/share/kelpie"}
KELPIE_CONFIG_HOME=${KELPIE_CONFIG_HOME:-"$HOME/.config/kelpie"}
KELPIE_BIN_DIR=${KELPIE_BIN_DIR:-"$HOME/.local/bin"}

mkdir -p "$KELPIE_HOME" "$KELPIE_CONFIG_HOME" "$KELPIE_BIN_DIR"

copy_file() {
  src=$1
  dst=$2
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
}

copy_dir() {
  src=$1
  dst=$2
  rm -rf "$dst"
  mkdir -p "$(dirname "$dst")"
  cp -R "$src" "$dst"
}

copy_file "$SCRIPT_DIR/AGENTS.md" "$KELPIE_HOME/AGENTS.md"
copy_file "$SCRIPT_DIR/Dockerfile.llm-base" "$KELPIE_HOME/Dockerfile.llm-base"
copy_file "$SCRIPT_DIR/compose.llm.yaml" "$KELPIE_HOME/compose.llm.yaml"
copy_file "$SCRIPT_DIR/README.md" "$KELPIE_HOME/README.md"
copy_file "$SCRIPT_DIR/llm-entrypoint.sh" "$KELPIE_HOME/llm-entrypoint.sh"
copy_dir "$SCRIPT_DIR/prompts" "$KELPIE_HOME/prompts"
copy_dir "$SCRIPT_DIR/skills" "$KELPIE_HOME/skills"
copy_dir "$SCRIPT_DIR/examples" "$KELPIE_HOME/examples"
copy_dir "$SCRIPT_DIR/scripts" "$KELPIE_HOME/scripts"

chmod +x \
  "$KELPIE_HOME/llm-entrypoint.sh" \
  "$KELPIE_HOME/scripts/run_issue_workflow.py" \
  "$KELPIE_HOME/scripts/run_issue_workflow_in_container.sh"

if [ ! -f "$KELPIE_CONFIG_HOME/runner_config.json" ]; then
  copy_file "$SCRIPT_DIR/examples/runner_config.json" "$KELPIE_CONFIG_HOME/runner_config.json"
fi
if [ ! -f "$KELPIE_CONFIG_HOME/instruction_staging.json" ]; then
  copy_file "$SCRIPT_DIR/examples/instruction_staging.json" "$KELPIE_CONFIG_HOME/instruction_staging.json"
fi
if [ ! -f "$KELPIE_CONFIG_HOME/compose.local.yaml" ]; then
  copy_file "$SCRIPT_DIR/compose.local.yaml" "$KELPIE_CONFIG_HOME/compose.local.yaml"
fi
if [ ! -f "$KELPIE_CONFIG_HOME/runner.env" ]; then
  copy_file "$SCRIPT_DIR/examples/runner.env.example" "$KELPIE_CONFIG_HOME/runner.env"
fi

cat >"$KELPIE_BIN_DIR/kelpie" <<EOF
#!/bin/sh
set -eu
export KELPIE_HOME="${KELPIE_HOME}"
export KELPIE_CONFIG_HOME="${KELPIE_CONFIG_HOME}"
exec "${KELPIE_HOME}/scripts/run_issue_workflow_in_container.sh" "\$@"
EOF
chmod +x "$KELPIE_BIN_DIR/kelpie"

printf 'Installed kelpie to:\n'
printf '  home:   %s\n' "$KELPIE_HOME"
printf '  config: %s\n' "$KELPIE_CONFIG_HOME"
printf '  bin:    %s\n' "$KELPIE_BIN_DIR"
printf '\n'
printf 'Add this to your shell profile if needed:\n'
printf '  export PATH="%s:$PATH"\n' "$KELPIE_BIN_DIR"
