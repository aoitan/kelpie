#!/usr/bin/env bash
set -Eeuo pipefail

PROJECTS_ROOT="${KELPIE_PROJECTS_ROOT:-/projects}"
WITH_VOCAL_EXTRAS=false

usage() {
    cat <<'EOF'
Usage: scripts/install_all_projects.sh [--with-vocal-extras]

Install the dependencies for the explicitly mounted workspace projects.

Options:
  --with-vocal-extras  Install VocalInsight's optional Demucs/Torch extras.
  -h, --help           Show this help.

The project mount root is controlled by KELPIE_PROJECTS_ROOT (default: /projects).
EOF
}

while (($# > 0)); do
    case "$1" in
        --with-vocal-extras)
            WITH_VOCAL_EXTRAS=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'unknown option: %s\n\n' "$1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

project_path() {
    printf '%s/%s' "${PROJECTS_ROOT}" "$1"
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf 'required command is missing: %s\n' "$1" >&2
        exit 2
    fi
}

require_project() {
    if [[ ! -d "$1" ]]; then
        printf 'project mount is missing: %s\n' "$1" >&2
        exit 2
    fi
}

run_in() {
    local label=$1
    local directory=$2
    shift 2
    printf '\n==> %s\n' "${label}"
    (cd "${directory}" && "$@")
}

require_command uv
require_command npm
require_command poetry
require_command cargo
require_command java
require_command sourcekitten

agent_cli=$(project_path multi-llm-agent-cli)
agent_cli_poc=$(project_path multi-llm-agent-cli-poc)
chat_root=$(project_path multi-llm-chat)
reviewer=$(project_path multi-llm-reviewer)
analyzer_mcp=$(project_path project-analyzer-mcp)
isohyps=$(project_path project-analyzer-rlm/isohyps)
tomoe=$(project_path project-analyzer-rlm/tomoe)
token_filter=$(project_path token_filter/repo)
vocal=$(project_path vocal_insight_ai)

chat_projects=(
    "${chat_root}/repo"
    "${chat_root}/codex"
    "${chat_root}/continue"
    "${chat_root}/gemini"
    "${chat_root}/copilot"
)

for project in \
    "${agent_cli}" \
    "${agent_cli_poc}" \
    "${reviewer}" \
    "${analyzer_mcp}" \
    "${isohyps}" \
    "${tomoe}" \
    "${token_filter}" \
    "${vocal}" \
    "${chat_projects[@]}"; do
    require_project "${project}"
done

run_in 'multi-llm-agent-cli: npm dependencies' "${agent_cli}" npm ci
run_in 'multi-llm-agent-cli-poc: npm dependencies' "${agent_cli_poc}" npm ci
run_in 'project-analyzer-mcp: npm dependencies' "${analyzer_mcp}" npm ci

run_in 'multi-llm-agent-cli-poc: Python environment' "${agent_cli_poc}" \
    uv venv --python 3.12 .venv
run_in 'multi-llm-agent-cli-poc: Python dependencies' "${agent_cli_poc}" \
    uv pip install --python "${agent_cli_poc}/.venv/bin/python" -r requirements.txt

for project in "${chat_projects[@]}"; do
    run_in "${project}: locked Python environment" "${project}" \
        uv sync --frozen --python 3.12 --extra dev
done

run_in 'multi-llm-reviewer: locked Python environment' "${reviewer}" \
    uv sync --frozen --python 3.13
run_in 'isohyps: locked Python environment with symbols extra' "${isohyps}" \
    uv sync --frozen --python 3.11 --extra symbols
run_in 'isohyps: test runner dependency' "${isohyps}" \
    uv pip install --python "${isohyps}/.venv/bin/python" pytest
run_in 'tomoe: locked Python environment' "${tomoe}" \
    uv sync --frozen --python 3.12

vocal_python=$(uv python find 3.12)
run_in 'vocal_insight_ai: select Python 3.12' "${vocal}" \
    poetry env use "${vocal_python}"
poetry_args=(install --no-ansi --no-interaction)
if [[ "${WITH_VOCAL_EXTRAS}" == true ]]; then
    poetry_args+=(--extras demucs)
fi
run_in 'vocal_insight_ai: locked Poetry environment' "${vocal}" poetry "${poetry_args[@]}"

run_in 'token_filter: locked Rust dependency fetch' "${token_filter}" \
    cargo fetch --locked

printf '\nAll project dependencies are installed.\n'
