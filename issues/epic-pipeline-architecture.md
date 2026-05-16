# Epic: 次世代パイプラインアーキテクチャ（Workflow / Runner 分離とLoopコンテナ化）

## 背景と目的

現在の Kelpie は、`run_issue_workflow.py` 側にフェーズの順序（planning -> implementation -> review_fix_loop等）や、I/O（成果物のファイル名）、プロンプト/スキルの呼び出しがハードコードされています。
そのため、フェーズごとの柔軟なモデル切り替えや、イテレーティブな反復処理（例: Coder -> Reviewer -> Fixer のループ）を表現することが困難です。

将来的に「設計と実装の分業」「TDDループの導入」など、高度なワークフローを構築・運用可能にするため、Kelpie の実行エンジンを「固定工程のランナー」から「**宣言的パイプラインエグゼキューター（Pipeline Executor）**」へと進化させる必要があります。

## アーキテクチャの方向性

1. **Runner と Workflow の分離**
   * `runners`: 純粋な「どうLLMを叩くか」（コマンド、モデル指定など）の定義
   * `workflows`: 各工程で「どのRunner/Skillを使い、何を入力として何を出力するか」のパイプライン構造の定義
2. **I/Oの明示化と仮想入力**
   * 各ステップに `inputs` と `outputs` を定義し、工程間の依存関係をデータとして可視化する。
   * `$issue`、`$loop_item` などの「仮想入力」を導入し、プロンプトへの動的なコンテキスト注入を簡潔に表現する。
3. **Prompt と Skill の分離**
   * `prompt`: タスクの指示内容そのもの（例: `prompts/04_solution_design.md`）
   * `skill`: 実行ルール・作法・制約（例: `skills/solution-design/SKILL.md`）
4. **Loop コンテナによる反復制御**
   * 単一ステップの属性として `strategy: iterative` を持たせるのではなく、`type: loop` となるステップコンテナを導入する。
   * Loop内に `coding` -> `review` -> `fix` などのサブパイプラインを定義する。
   * アイテムごとに名前空間（例: `work-items/{id}/...`）を自動分離し、アーティファクトの上書きによる状態破壊を防ぐ。

## 段階的な移行プラン (Phases)

いきなり全ワークフローをYAML/JSONのDSL化すると実装が破綻するため、以下の順序で段階的にリファクタリングを進めます。

### v1: `planning` を割る
現状の `planning` を `solution_design` と `work_breakdown` に分割する。

### v2: `work_items.json` を標準成果物化
`work_breakdown` の出力として、機械可読なJSON（Todoリスト）を出力させる。ただし、実装フェーズ自体はまだ1回呼び出しのままとする。

### v3: `run_phase` を `run_step` に抽象化
トップレベルのステップも、後から導入するループ内のステップも同じ呼び出し関数を使えるように実行エンジンのインターフェースを整理する。

### v4: `implementation` だけをloop化
全ワークフローのDSL化を行う前に、まず Python スクリプト内で固定で「`work_items.json` を回して `implementation_coding` を実行する」ループ処理を組み込む。

### v5: Reviewer / Fix step を足す
ループ内に `coder → reviewer → fix` の反復パイプラインを本格的に導入する。

### v6: workflow_config 外出し
最後に、Pythonスクリプト内の固定フェーズ定義を完全に削除し、外部のパイプライン定義ファイル（YAML等）へ読み込みを移行する。
