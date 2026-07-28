#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
IMAGE=${KELPIE_OPENCODE_TEST_IMAGE:-repo-llm}
FIXTURE_DIR="$REPO_ROOT/tests/fixtures/opencode"
TEST_ROOT=$(mktemp -d)
MOCK_PID=""

cleanup() {
  if [ -n "$MOCK_PID" ]; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT INT TERM

mkdir -p "$TEST_ROOT/workspace/.data"
cp "$FIXTURE_DIR/conflicting-project-opencode.json" "$TEST_ROOT/workspace/opencode.json"

node "$FIXTURE_DIR/mock_ollama.mjs" >"$TEST_ROOT/mock.log" 2>&1 &
MOCK_PID=$!

attempt=0
while [ "$attempt" -lt 50 ]; do
  if grep -q "listening" "$TEST_ROOT/mock.log"; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 0.1
done
if ! grep -q "listening" "$TEST_ROOT/mock.log"; then
  echo "OpenCode container test: mock server failed to start" >&2
  exit 1
fi

output=$(
  printf '%s\n' "Reply only after reading KELPIE_STDIN_MARKER" |
    docker run --rm -i \
      -e KELPIE_OPENCODE_CONFIG=/kelpie-config/mock-opencode.json \
      -e KELPIE_OPENCODE_STATE_DIR=/workspace/.data/opencode \
      -v "$FIXTURE_DIR:/kelpie-config:ro" \
      -v "$TEST_ROOT/workspace:/workspace" \
      -w /workspace \
      "$IMAGE" \
      kelpie-opencode run --pure --agent kelpie-artifact
)

printf '%s\n' "$output"
printf '%s\n' "$output" | grep -q "KELPIE_STDIN_CONFIRMED"
test -d "$TEST_ROOT/workspace/.data/opencode/data"
test -d "$TEST_ROOT/workspace/.data/opencode/cache"
test -d "$TEST_ROOT/workspace/.data/opencode/state"

echo "OpenCode container integration: passed"
