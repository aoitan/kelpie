# Task: examples/runner_config.json に複合的なランナー設定の例を追加する

## Summary

`examples/runner_config.json` に、フェーズごとに異なるCLI（例: gemini, codex, copilot）を使い分ける複合的なランナー設定 (`hybrid_cli`) の例を追加します。

## Goal

- `examples/runner_config.json` に `hybrid_cli` の設定例を追加する。
- ユーザーがフェーズごとのオーバーライド機能の強力なユースケースを理解できるようにする。

## Non-Goals

- 既存のランナー設定 (`gemini`, `codex`, `copilot`, `custom_file_prompt`) を変更すること。
- kelpie 本体のコードやスキーマを変更すること。

## Constraints

- 追加する設定は JSON フォーマットとして正しいこと。
- `issues/phase-overrides-runner-config.md` に記載されている `phase_overrides` の仕様に準拠すること。

## Current Problem

- 現状の `examples/runner_config.json` には、フェーズごとに全く異なる CLI を呼び出すような複合的な設定例が存在しないため、機能の有用性が伝わりにくい。

## Expected Outcome

- `examples/runner_config.json` の `runners` オブジェクト内に `hybrid_cli` が追加されている。

## Validation

- `examples/runner_config.json` が正しい JSON フォーマットであることを確認する。
- `hybrid_cli` の設定に `phase_overrides` が含まれ、`implementation` や `review_fix_loop` などのフェーズで異なるコマンドが指定されていることを確認する。

## Notes

- 追加する設定の具体例：
  ```json
    "hybrid_cli": {
      "command_template": ["gemini", "--yolo", "-p", ""],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "implementation": {
          "command_template": ["codex", "exec", "--model", "gpt-5-codex", "--full-auto", "-"],
          "prompt_mode": "stdin"
        },
        "review_fix_loop": {
          "command_template": ["copilot", "--allow-all-tools", "--silent"]
        }
      }
    }
  ```
