#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage:
  run_issue_workflow_in_container.sh [options] -- --issue 12 --runner codex [run_issue_workflow args...]

Options:
  --kelpie-home PATH      Template/install directory. Default: $KELPIE_HOME or inferred from script location
  --config-home PATH      Config directory. Default: $KELPIE_CONFIG_HOME or ~/.config/kelpie
  --target-workdir PATH   Host path of the repository to operate on. Default: current directory
  --data-dir PATH         Host path to mount as /workspace/.data. Default: <target-workdir>/.data
  --no-build              Skip docker compose build
  -h, --help              Show this help

Everything after `--` is passed to run_issue_workflow.py.

Example:
  run_issue_workflow_in_container.sh \
    --target-workdir /path/to/target-repo \
    -- \
    --issue 12 \
    --issue-source github \
    --github-repo owner/repo \
    --include-issue-comments \
    --runner codex
EOF
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_KELPIE_HOME=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
KELPIE_HOME=${KELPIE_HOME:-$DEFAULT_KELPIE_HOME}
KELPIE_CONFIG_HOME=${KELPIE_CONFIG_HOME:-"$HOME/.config/kelpie"}
TARGET_WORKDIR=$(pwd)
DATA_DIR=""
DO_BUILD=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --kelpie-home)
      KELPIE_HOME=$2
      shift 2
      ;;
    --config-home)
      KELPIE_CONFIG_HOME=$2
      shift 2
      ;;
    --target-workdir)
      TARGET_WORKDIR=$2
      shift 2
      ;;
    --data-dir)
      DATA_DIR=$2
      shift 2
      ;;
    --no-build)
      DO_BUILD=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ "$#" -eq 0 ]; then
  echo "Missing run_issue_workflow.py arguments." >&2
  usage >&2
  exit 1
fi

KELPIE_HOME=$(cd "$KELPIE_HOME" && pwd)
if [ -d "$KELPIE_CONFIG_HOME" ]; then
  KELPIE_CONFIG_HOME=$(cd "$KELPIE_CONFIG_HOME" && pwd)
else
  mkdir -p "$KELPIE_CONFIG_HOME"
  KELPIE_CONFIG_HOME=$(cd "$KELPIE_CONFIG_HOME" && pwd)
fi
TARGET_WORKDIR=$(cd "$TARGET_WORKDIR" && pwd)
if [ -z "$DATA_DIR" ]; then
  DATA_DIR="$TARGET_WORKDIR/.data"
fi
mkdir -p "$DATA_DIR"
DATA_DIR=$(cd "$DATA_DIR" && pwd)

cd "$KELPIE_HOME"

COMPOSE_FILE_1="$KELPIE_HOME/compose.llm.yaml"
COMPOSE_FILE_2=""
if [ -f "$KELPIE_CONFIG_HOME/compose.local.yaml" ]; then
  COMPOSE_FILE_2="$KELPIE_CONFIG_HOME/compose.local.yaml"
elif [ -f "$KELPIE_HOME/compose.local.yaml" ]; then
  COMPOSE_FILE_2="$KELPIE_HOME/compose.local.yaml"
fi
RUNNER_CONFIG_PATH="/opt/kelpie/examples/runner_config.json"
if [ -f "$KELPIE_CONFIG_HOME/runner_config.json" ]; then
  RUNNER_CONFIG_PATH="/kelpie-config/runner_config.json"
fi
INSTRUCTION_STAGING_PATH="/opt/kelpie/examples/instruction_staging.json"
if [ -f "$KELPIE_CONFIG_HOME/instruction_staging.json" ]; then
  INSTRUCTION_STAGING_PATH="/kelpie-config/instruction_staging.json"
fi

if [ "$DO_BUILD" -eq 1 ]; then
  if [ -n "$COMPOSE_FILE_2" ]; then
    env LLM_BUILD_CONTEXT="$KELPIE_HOME" LLM_WORKSPACE="$TARGET_WORKDIR" LLM_DATA_DIR="$DATA_DIR" \
      docker compose -f "$COMPOSE_FILE_1" -f "$COMPOSE_FILE_2" build llm
  else
    env LLM_BUILD_CONTEXT="$KELPIE_HOME" LLM_WORKSPACE="$TARGET_WORKDIR" LLM_DATA_DIR="$DATA_DIR" \
      docker compose -f "$COMPOSE_FILE_1" build llm
  fi
fi

if [ -n "$COMPOSE_FILE_2" ]; then
  env LLM_BUILD_CONTEXT="$KELPIE_HOME" LLM_WORKSPACE="$TARGET_WORKDIR" LLM_DATA_DIR="$DATA_DIR" \
    docker compose -f "$COMPOSE_FILE_1" -f "$COMPOSE_FILE_2" run --rm \
      -v "$KELPIE_CONFIG_HOME:/kelpie-config:ro" \
      llm \
      run_issue_workflow.py \
      --repo-root /opt/kelpie \
      --workdir /workspace \
      --runner-config "$RUNNER_CONFIG_PATH" \
      --instruction-staging-config "$INSTRUCTION_STAGING_PATH" \
      "$@"
else
  env LLM_BUILD_CONTEXT="$KELPIE_HOME" LLM_WORKSPACE="$TARGET_WORKDIR" LLM_DATA_DIR="$DATA_DIR" \
    docker compose -f "$COMPOSE_FILE_1" run --rm \
      -v "$KELPIE_CONFIG_HOME:/kelpie-config:ro" \
      llm \
      run_issue_workflow.py \
      --repo-root /opt/kelpie \
      --workdir /workspace \
      --runner-config "$RUNNER_CONFIG_PATH" \
      --instruction-staging-config "$INSTRUCTION_STAGING_PATH" \
      "$@"
fi
