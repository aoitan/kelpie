#!/usr/bin/env bash
set -u -o pipefail

PROJECTS_ROOT="${KELPIE_PROJECTS_ROOT:-/projects}"
failures=0

project_path() {
    printf '%s/%s' "${PROJECTS_ROOT}" "$1"
}

run_step() {
    local label=$1
    local directory=$2
    shift 2

    printf '\n==> %s\n' "${label}"
    if [[ ! -d "${directory}" ]]; then
        printf 'SKIP: project mount is missing: %s\n' "${directory}" >&2
        failures=$((failures + 1))
        return 0
    fi
    if (cd "${directory}" && "$@"); then
        printf 'PASS: %s\n' "${label}"
    else
        printf 'FAIL: %s\n' "${label}" >&2
        failures=$((failures + 1))
    fi
}

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

run_step 'multi-llm-agent-cli: TypeScript build' "${agent_cli}" npx tsc
run_step 'multi-llm-agent-cli: Jest tests' "${agent_cli}" npm test -- --runInBand

run_step 'multi-llm-agent-cli-poc: TypeScript build' "${agent_cli_poc}" npm run build
run_step 'multi-llm-agent-cli-poc: Jest and Python tests' "${agent_cli_poc}" \
    env "PATH=${agent_cli_poc}/.venv/bin:${PATH}" npm test -- --runInBand

for project in "${chat_projects[@]}"; do
    run_step "${project}: package build" "${project}" uv build
    run_step "${project}: pytest" "${project}" \
        env MULTI_LLM_CHAT_MCP_ENABLED=false uv run pytest -q
done

run_step 'multi-llm-reviewer: package build' "${reviewer}" uv build
run_step 'multi-llm-reviewer: pytest' "${reviewer}" uv run pytest -q

run_step 'project-analyzer-mcp: TypeScript build' "${analyzer_mcp}" npm run build
run_step 'project-analyzer-mcp: Kotlin fat-jar build' "${analyzer_mcp}" npm run build-kotlin-parser-cli
run_step 'project-analyzer-mcp: full test suite' "${analyzer_mcp}" npm run test:all

run_step 'isohyps: Python compile check' "${isohyps}" \
    uv run python -m compileall -q analyzer.py rlm_cli.py isohyps tests
run_step 'isohyps: pytest' "${isohyps}" uv run pytest -q

run_step 'tomoe: package build' "${tomoe}" uv build
run_step 'tomoe: pytest' "${tomoe}" uv run pytest -q

run_step 'token_filter: rustfmt check' "${token_filter}" cargo fmt --check
run_step 'token_filter: Cargo build' "${token_filter}" cargo build --locked
run_step 'token_filter: Cargo tests' "${token_filter}" cargo test --locked

run_step 'vocal_insight_ai: Poetry lock check' "${vocal}" poetry check --lock
run_step 'vocal_insight_ai: package build' "${vocal}" poetry build
run_step 'vocal_insight_ai: pytest' "${vocal}" poetry run pytest -q

if ((failures > 0)); then
    printf '\n%d project check(s) failed.\n' "${failures}" >&2
    exit 1
fi

printf '\nAll configured project checks passed.\n'
