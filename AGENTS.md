# AGENTS.md

このリポジトリでは、CLIエージェントに「1工程ずつ」仕事をさせる。

目的は、単発の巨大プロンプトで全部やらせるのではなく、
以下の工程を順番に実行し、各工程の成果物を明示的に残すこと。

1. prototype planning
2. prototyping
3. red team review
4. solution design
5. work breakdown
6. implementation
7. review/fix loop
8. pull request

---

## 基本ルール

- 常に **Issue** または **Manual Task** を起点に作業する。
- Issue がある場合、主ソースは **GitHub Issues** を優先する。
- 必要に応じて Issue コメントも入力に含める。Issue がない場合は手動タスク文脈、関連コード、既存ドキュメントを入力に含める。
- 1回の工程では **その工程の責務だけ** を実施する。
- 次工程に必要な情報は、必ず成果物として `.kelpie/artifacts/` に残す。
- 不明点があっても作業停止を最小化し、妥当な仮定を明示して前進する。
- 破壊的変更・依存追加・権限変更・外部送信を伴う場合は理由を成果物に記録する。
- 実装より前に、少なくとも一度は失敗条件・非目標・既知リスクを書く。
- 各工程では、対応する `skills/<phase>/SKILL.md` を必ず読む前提で行動する。
- 各工程終了時に、最低限以下を出力する:
  - 何をやったか
  - 何をやっていないか
  - 次工程への入力
  - リスク / 未解決事項

---

## ディレクトリ規約

```text
.kelpie/
  .gitignore
  instructions/
  artifacts/
    github/
      owner/
        repo/
          issue-xx/
            01-prototype-planning.md
            02-prototype-summary.md
            03-red-team-review.md
            04-solution-design.md
            05-work-breakdown.md
            06-implementation-notes.md
            07-review-fix-loop.md
            08-pr-draft.md
            .issue-cache/
              issue.json
              issue_comments.json
            intent-records/
            checks/
    manual/
      local/
        task-xxxx/
          01-prototype-planning.md
          02-prototype-summary.md
          03-red-team-review.md
          04-solution-design.md
          05-work-breakdown.md
          06-implementation-notes.md
          07-review-fix-loop.md
          08-pr-draft.md
          intent-records/
          checks/
```

`.kelpie/artifacts/.../issue-xx/` または `.kelpie/artifacts/.../task-xxxx/` 配下に工程ごとの成果物を残す。

---

## 工程ごとの責務

### 1) prototype planning
目的:
- 問題の理解を揃える
- 最小プロトタイプの範囲を決める
- 成否判定を簡単に定義する

出力:
- `01-prototype-planning.md`

### 2) prototyping
目的:
- 捨てやすい実験で見通しを得る

出力:
- `02-prototype-summary.md`
- 必要なら試作コード

### 3) red team review
目的:
- 試作や計画の危険点を先に炙る

出力:
- `03-red-team-review.md`

### 4) solution design
目的:
- 本実装に向けた全体設計を固める

出力:
- `04-solution-design.md`

やること:
- 設計方針の決定
- アーキテクチャ設計
- 主要インターフェースの定義

### 5) work breakdown
目的:
- 設計をタスクに分解し、実装計画を立てる

出力:
- `05-work-breakdown.md`

やること:
- タスク分割
- 依存関係整理
- 実装順序決定
- 完了条件の明記

### 6) implementation
目的:
- 計画に従って実装する

出力:
- コード変更
- `06-implementation-notes.md`

やること:
- 1回で全部盛りしない
- 計画との差分が出たら理由を書く
- 追加したファイル、主要変更点、未対応点を残す

### 7) review/fix loop
目的:
- 実装品質を上げる

出力:
- `07-review-fix-loop.md`

### 8) pull request
目的:
- 人間がレビューしやすいPR材料を揃える

出力:
- `08-pr-draft.md`

やること:
- 変更概要
- 背景
- 変更点
- テスト
- 残課題
- レビューポイント

---

## エージェントへの共通指示

- 大きく迷ったら、抽象議論を伸ばしすぎず、現時点で妥当な選択肢を1つ採る。
- 不確実性は消さずに記録する。
- できるだけ小さい差分を積む。
- GitHub Issue 本文とコメントがある場合は、そのうち実装判断に効く文脈を優先して使う。
- Issue がない、または Issue だけで足りない場合は、リポジトリ内の関連コード・既存ドキュメント・過去成果物で補完する。
- 自動チェック可能な点は常に意識する。
- 将来 `Intent Record` と `機械チェック` が差し込まれる前提で、判断理由を工程単位で残す。

---

## 各工程で参照する入力

- GitHub Issue 本文、または Manual Task Context
- 必要に応じて GitHub Issue コメント
- `AGENTS.md`
- 対応工程の `skills/<phase>/SKILL.md`
- 対応工程の `prompts/*.md`
- それ以前の工程で生成された `.kelpie/artifacts/.../issue-xx/*` または `.kelpie/artifacts/.../task-xxxx/*`

---

## 失敗時の扱い

- 失敗を隠さない。
- 途中で詰まった場合も、
  - どこまで進んだか
  - 何が障害か
  - 次に人間が判断すべき点
  を成果物に残して終了する。
