## 背景
Epic #1 の v1 ステップ。現状の planning フェーズが「全体設計」と「タスク分割」を同時に行っており、コンテキストが混線しやすいため、これを明示的に分割する。

## ゴール
- `run_issue_workflow.py` に `solution_design` と `work_breakdown` フェーズを追加。
- 既存の `planning` フェーズをこれらに置き換える。
- それぞれに対応するデフォルトプロンプト（`prompts/04_solution_design.md`, `prompts/05_work_breakdown.md`）を用意する。

## 受入条件
- 実行時に「設計」と「タスク分割」が別々のプロンプトで実行されること。
- `solution_design` の出力が `work_breakdown` の入力として正しく渡されること。
