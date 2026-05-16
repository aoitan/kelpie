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

