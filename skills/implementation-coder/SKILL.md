---
name: implementation-coder
description: Implement one planned work item with a small, reviewable diff.
---

# SKILL: implementation-coder

## 目的

1つの work item を計画に沿って実装し、reviewer が確認できる変更と記録を残す。

## 守ること

- Issue / Manual Task と work item の受け入れ条件から外れない
- 変更範囲を work item の files と依存する最小範囲に限定する
- 既存の公開契約や他の work item の成果物を無断で置き換えない
- 実装前後に必要なローカル確認を行い、失敗を隠さない
- `06-implementation-notes.md` に計画との差分、未対応事項、確認内容を残す
- reviewer の専用成果物 `review-result.json` を作らない、修正しない

## 出力契約

- 実装の変更は対象 repository に反映する
- 実装 notes は現在の Artifact Directory に書く
- 共通の Required Phase Outcome で指定された implementation outcome JSON を最後に書く

## 禁止事項

- 無関係な改善や大規模なリファクタリング
- reviewerの代わりの合否判定
- `review-result.json` の生成
- 明示されていない外部送信、commit、push
