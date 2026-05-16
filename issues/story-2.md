## 背景
Epic #1 の v2 ステップ。後続のイテレーティブ実装（v4）のために、タスク分割の結果を構造化データとして保存する必要がある。

## ゴール
- `work_breakdown` フェーズのプロンプトを調整し、タスクリストを JSON 形式（`work_items.json`）で出力させる。
- `run_issue_workflow.py` で、この JSON ファイルを成果物として保存・管理する仕組みを導入する。

## 受入条件
- `work_breakdown` 終了後に `.kelpie/artifacts/` 配下に有効な `work_items.json` が生成されていること。
- JSON には各タスクの `id`, `title`, `description` などが含まれていること。
