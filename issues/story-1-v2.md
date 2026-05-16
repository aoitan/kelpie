## 背景
Epic #1 の v1 ステップ。現状の planning フェーズが「全体設計」と「タスク分割」を同時に行っており、コンテキストが混線しやすいため、これを明示的に分割する。

## ゴール
- `run_issue_workflow.py` に `solution_design` と `work_breakdown` フェーズを追加。
- 既存の `planning` フェーズをこれらに置き換える。
- それぞれに対応するデフォルトプロンプトを用意する。

## 実装上の注意点 (Implementation Notes)
- **Prompt と Skill の分離**: 呼び出し時に `prompt` (タスク指示) と `skill` (作法・制約) を明確に分けて指定できるようにする。
- **成果物のリレー**: `solution_design` の出力（設計書）が `work_breakdown` の主入力となるよう、データの流れを整理する。

## 受入条件
- 実行時に「設計」と「タスク分割」が別々のプロンプトで実行されること。
- `solution_design` の出力が `work_breakdown` の入力として正しく渡されること。
