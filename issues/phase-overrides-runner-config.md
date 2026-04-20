# Runner Phase Overrides Specification

## 背景

各工程で使いたいモデルが異なるケースがある。
例:

- `prototype_planning` は広い整理に向いたモデルを使いたい
- `implementation` はコード変更に強いモデルを使いたい
- `review_fix_loop` は再レビューに強いモデルを使いたい

現状の `examples/runner_config.json` は runner ごとに単一の `command_template` しか持てないため、工程ごとの切り替えができない。

## 目的

- 各 phase ごとに CLI 起動設定を切り替えられるようにする
- 既存の runner 設定を壊さない
- CLI ごとの差異を kelpie 本体に持ち込みすぎない

## 非目的

- `model` という抽象キーを kelpie が共通解釈すること
- CLI 間で共通のモデル指定ルールを定義すること
- `github` / `file` / `none` の入力ソース仕様を変えること

## 最小仕様

`runner_config.json` の各 runner に、省略可能な `phase_overrides` を追加する。

- `phase_overrides` は省略可能
- `phase_overrides` がない場合は従来どおり runner 直下の設定を使う
- 対象 phase に override がない場合も従来どおり runner 直下の設定を使う
- override できるのはまず `command_template` と `prompt_mode` のみ

## 設定例

```json
{
  "runners": {
    "codex": {
      "command_template": ["codex", "exec", "--full-auto", "-"],
      "prompt_mode": "stdin",
      "phase_overrides": {
        "prototype_planning": {
          "command_template": ["codex", "exec", "--model", "gpt-5.4", "--full-auto", "-"]
        },
        "implementation": {
          "command_template": ["codex", "exec", "--model", "gpt-5-codex", "--full-auto", "-"]
        },
        "review_fix_loop": {
          "command_template": ["codex", "exec", "--model", "gpt-5.4", "--full-auto", "-"]
        }
      }
    }
  }
}
```

## 解決ルール

phase 実行時の runner 設定は次の順で解決する。

1. runner 直下の `command_template`
2. runner 直下の `prompt_mode`
3. `phase_overrides.<phase>.command_template` があれば上書き
4. `phase_overrides.<phase>.prompt_mode` があれば上書き

## 互換性

- `phase_overrides` を省略した既存設定はそのまま動く
- phase ごとの override は必要な工程だけ書けばよい
- 未指定 phase はデフォルト設定を継続利用する

## 実装メモ

- `RunnerConfig` に `phase_overrides` を追加する
- `invoke_cli()` の前に、phase ごとの有効設定を解決する
- `prompt_mode` の許容値は現状どおり `stdin | arg | file`
- README には phase ごとのモデル切り替え例を追記する

## Manual Task 文面テンプレート

`--issue-source file` で使うローカル task / issue 文書は、最低限次の形を推奨する。

```md
# Task: <short title>

## Summary

<何を変えたいかを 2〜5 行で書く>

## Goal

- <達成したいこと>
- <達成したいこと>

## Non-Goals

- <今回やらないこと>
- <今回やらないこと>

## Constraints

- <使うべき技術や避けるべき変更>
- <後方互換や運用上の制約>

## Current Problem

- <困っている挙動や現状の制約>
- <関連しそうなファイルや仕組み>

## Expected Outcome

- <完了時にどうなっていればよいか>

## Validation

- <どう確認するか>
- <最低限必要なテストや手確認>

## Notes

- <補足>
```

## 文面テンプレートの使い方

- `Summary` は短く具体的に書く
- `Goal` と `Non-Goals` を両方書く
- `Constraints` があると phase の脱線を抑えやすい
- `Validation` を書いておくと planning 以降が安定する
- 完璧でなくてよいが、少なくとも `Summary` と `Goal` は埋める
